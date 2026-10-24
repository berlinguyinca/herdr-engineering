// Collaboration rooms: sign in with the BinView user pool, create rooms, chat, and invite
// people by email. Remote messages use the shared safe Markdown renderer.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { bundleScript, filesFromBulk, shq, workerScript } from './collab-scripts.js';
import { renderFlow } from './collab-flow.js';
import {
  createHelpersPanel, renderEgressRequests, renderHelperTree, renderHostsTable, renderPanic, renderProfileEditor, wireHelperHandlers,
} from './collab-helpers.js';
import { ensureConversationStyles, mountComposer } from './conversation-composer.js';
import { renderMarkdown } from './conversation-markdown.js';
import {
  AuthError, CollabApi, TokenStore, authorizeUrl, collabRouteState, exchangeCode, parseRedirect, pkcePair, randomString,
} from './herdr-web-client/collab.js';

const ROLES = [['contributor', 'Can chat'], ['viewer', 'Read only'], ['guest', 'Guest (read only, expires)']];
const PERMS = [['read', 'Read messages'], ['post', 'Write messages'], ['execute', 'Receive tasks'], ['instruct', 'Send tasks to agents'], ['spawn_helpers', 'Start helper agents']];
const PERM_SHORT = { read: 'Read', post: 'Write', execute: 'Receive tasks', instruct: 'Send tasks', spawn_helpers: 'Start helpers' };
const ROLE_LABEL = { owner: 'Owner', contributor: 'Can chat', viewer: 'Read only', guest: 'Guest' };

async function loadConfig() {
  const res = await fetch('/api/v1/collab/config');
  if (!res.ok) return null;
  const cfg = await res.json();
  return cfg && cfg.client_id && cfg.hosted_ui_domain && cfg.public_url ? cfg : null;
}

// Called once at page load. If Cognito just redirected back with a code, finish sign-in.
export async function completeSignInIfRedirected() {
  const store = new TokenStore(window.sessionStorage);
  const found = parseRedirect(window.location.search);
  if (!found) return false;
  const done = (hash) => window.history.replaceState(null, '', `${window.location.pathname}#${hash}`);
  const saved = JSON.parse(window.sessionStorage.getItem('hcn.pkce') || 'null');
  window.sessionStorage.removeItem('hcn.pkce');
  if (found.error || !saved || saved.state !== found.state) {
    window.sessionStorage.setItem('hcn.error', found.error || 'sign-in could not be verified; try again');
    done('/collab');
    return true;
  }
  try {
    const cfg = await loadConfig();
    store.set(await exchangeCode(cfg, found.code, saved.verifier, cfg.public_url));
  } catch (e) {
    window.sessionStorage.setItem('hcn.error', e.message);
  }
  done('/collab');
  return true;
}

const permBoxes = (cur, name) => PERMS.map(([v, l]) => `<label class="cx-check"><input type="checkbox" name="${name}" value="${v}"${cur.includes(v) ? ' checked' : ''}> ${esc(l)}</label>`).join('');
const picked = (form, name) => [...form.querySelectorAll(`input[name="${name}"]:checked`)].map((i) => i.value);
const pill = (text, color = 'var(--text-muted)') =>
  `<span class="hd-status" style="--sc:${color}"><span class="hd-status__dot" aria-hidden="true">●</span><span class="hd-status__label">${esc(text)}</span></span>`;
const roleLabel = (r) => ROLE_LABEL[r] || r;
const shortId = (id) => `…${String(id || '').slice(-6)}`;
const hue = (s) => { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
// An agent's name as a coloured badge, with a pencil for those allowed to rename it.
const agentBadge = (name) => `<span class="cx-av cx-av--sm" style="--h:${hue(name)}" aria-hidden="true">${esc((String(name).match(/[a-z0-9]/gi) || ['?'])[0].toUpperCase())}</span>`;
const agentTitle = (a, canRename) => `<span class="cx-ntitle">${agentBadge(a.name)}<b class="cx-nname">${esc(a.display_name || `@${a.name}`)}</b>${a.display_name?`<small class="hd-ident">@${esc(a.name)}</small>`:''}${canRename ? `<button type="button" class="cx-edit" data-rename="${esc(a.agent_id)}" data-name="${esc(a.name)}" title="Rename @ handle" aria-label="Rename @${esc(a.name)} handle">✎</button>` : ''}</span>`;

// One credential, shown once, in the format each client wants. Secrets only ever go through textContent.
const connectSnippets = (cfg, a) => {
  const url = new URL('/mcp', cfg.public_url).href;
  const auth = `Agent ${a.credential}`;
  return [
    { name: 'Claude Code', text: `claude mcp add --transport http herdr-collab ${url} --header "Authorization: ${auth}" --header "X-Herdr-Device: ${a.device_id}"`,
      note: 'Run once in a terminal. Check it with: claude mcp list' },
    { name: 'Codex', text: `# add to ~/.codex/config.toml\n[mcp_servers.herdr-collab]\nurl = "${url}"\nhttp_headers = { "Authorization" = "${auth}", "X-Herdr-Device" = "${a.device_id}" }`,
      note: 'Needs a Codex version with streamable-HTTP MCP support. Restart Codex afterwards.' },
    { name: 'Pi and others', text: JSON.stringify({ mcpServers: { 'herdr-collab': { type: 'http', url, headers: { Authorization: auth, 'X-Herdr-Device': a.device_id } } } }, null, 2),
      note: `Any client that speaks MCP over streamable HTTP works. If yours has no config file, give it the URL ${url} and the two headers.` },
  ];
};

const GUIDE = `<div class="hd-card">
  <div class="hd-card__title">How to connect your AI tools</div>
  <ol class="cx-steps">
    <li><b>Connect an agent</b> below. You get a private credential, shown once.</li>
    <li><b>Paste the snippet</b> for your tool (Claude Code, Codex, Pi or any MCP client).</li>
    <li><b>Add the agent to a room</b> from that room's page. Until you do, it can see nothing.</li>
    <li>The agent can then <b>list rooms, read messages and post</b> as you, in those rooms only. It cannot run commands on your machine through this connection, and everything it reads from others is marked untrusted.</li>
    <li><b>Revoke</b> it here any time; it stops working immediately. Credentials expire after 30 days.</li>
  </ol></div>`;

const USER_PILL = { pending: ['Waiting', 'var(--status-attention, #d89b1d)'], approved: ['Approved', 'var(--status-ok, #1a9b6c)'],
  denied: ['Declined', 'var(--text-muted)'], disabled: ['Disabled', 'var(--status-danger, #c0392b)'], kicked: ['Removed', 'var(--status-danger, #c0392b)'] };
const when = (iso) => (iso ? String(iso).replace('T', ' ').slice(0, 16) : '');

export function renderPeople({ users = [], manage = false, filter = 'pending', showLog = false, counts = {} }) {
  const chips = manage ? ['pending', 'approved', 'disabled', 'denied', 'kicked', 'all'] : ['pending'];
  const rows = users.filter((u) => filter === 'all' || u.status === filter);
  const buttons = (u) => {
    const b = (act, label, cls = '') => `<button type="button" class="hd-btn hd-btn--sm${cls}" data-act="${act}" data-user="${esc(u.user_id)}">${label}</button>`;
    if (u.status === 'pending') return b('approve', 'Approve') + b('deny', 'Decline', ' hd-btn--ghost') + (manage ? b('delete', 'Delete person permanently', ' hd-btn--danger') : '');
    if (!manage) return '';
    if (u.status === 'approved') return b('disable', 'Disable', ' hd-btn--ghost') + b('delete', 'Delete person permanently', ' hd-btn--danger');
    if (u.status === 'disabled') return b('enable', 'Enable') + b('delete', 'Delete person permanently', ' hd-btn--danger');
    return b('approve', 'Approve again', ' hd-btn--ghost') + b('delete', 'Delete person permanently', ' hd-btn--danger');
  };
  return `<div class="cx-head"><div><h1>People</h1>
    <p class="cx-sub">${manage ? 'Everyone who has signed in. Nobody can use rooms or agents until approved; you can disable access or delete a person permanently.' : 'People waiting to join. Approve the ones you know; they get access straight away.'}</p></div></div>
    <div class="cx-chips" role="tablist" aria-label="Filter people">${chips.map((c) => `<button type="button" class="cx-chip" role="tab" data-filter="${c}" aria-selected="${c === filter}">${c === 'all' ? 'Everyone' : (USER_PILL[c] || [c])[0]}${c !== 'all' && counts[c] ? ` · ${counts[c]}` : ''}</button>`).join('')}</div>
    <label class="cx-field">Note for the log (optional)<input id="hcn-user-note" maxlength="200" placeholder="e.g. joins the MS team"></label>
    ${rows.length ? `<ul class="cx-list" id="hcn-users">${rows.map((u) => `<li>
      <span class="grow"><b>${esc(u.email || shortId(u.user_id))}</b> ${pill(...(USER_PILL[u.status] || [u.status]))}
        <span class="hd-card__sub">${u.status === 'pending' ? `asked ${esc(when(u.since))}` : `${esc(u.decided_by || '')} ${esc(when(u.decided_at))}`}${manage && u.status === 'approved' ? ` · ${u.rooms} room${u.rooms === 1 ? '' : 's'}, ${u.agents} agent${u.agents === 1 ? '' : 's'}` : ''}${u.reason ? ` · “${esc(u.reason)}”` : ''}</span></span>
      <span class="cx-row">${buttons(u)}</span></li>`).join('')}</ul>`
      : `<div class="hd-empty">${filter === 'pending' ? 'Nobody is waiting.' : 'Nobody here.'}</div>`}
    ${manage ? `<section class="cx-section"><h2>Decision log</h2><button id="hcn-log-btn" class="hd-btn hd-btn--ghost hd-btn--sm" type="button">${showLog ? 'Hide' : 'Show'} the log</button><div id="hcn-log"></div></section>` : ''}
    <span id="hcn-people-msg" class="cx-msg" role="status"></span>`;
}

const confirmAction = (message) => typeof globalThis.confirm === 'function' && globalThis.confirm(message);
export function confirmDeletePerson(person) {
  return confirmAction(`Delete ${person.email || person.user_id} permanently? All memberships, sessions, tokens, invitations, owned-agent records and authored messages will be removed. Identity and authored content will be scrubbed from live rooms and retained archives; only minimal audit metadata survives. Room-owned files will remain, with uploader attribution removed or anonymized. This cannot be undone.`);
}
export function confirmDeleteRoom(room) {
  return confirmAction(`Archive and delete ${room.name}? Room data, messages, files and previews are archived and verified before live data is removed. The verified archive is retained; restore creates a new room ID. A failed archive leaves the live room intact.`);
}

export function renderRoomAdministration({ room, invites = [], canManage = false }) {
  if (!canManage) return '';
  const members = (room.members || []).filter(m => m.room_membership_status !== 'removed');
  return `<ul class="cx-list">${members.map(m => {
    const pending = m.room_membership_status === 'pending';
    const approved = !pending && (m.user_status ?? 'approved') === 'approved' && (m.room_membership_status ?? 'approved') === 'approved';
    return `<li><span class="grow">${esc(m.email || shortId(m.user_id))}</span>${pill(pending ? 'Waiting for room approval' : roleLabel(m.role))}
      ${pending ? `<button type="button" class="hd-btn hd-btn--sm" data-approve-member="${esc(m.user_id)}">Approve member</button>` : ''}
      ${approved ? `<label class="cx-check"><input type="checkbox" data-upload="${esc(m.user_id)}"${m.file_upload_permission === true ? ' checked' : ''}${m.role === 'owner' ? ' disabled' : ''}> Upload files</label>` : ''}
      ${m.role !== 'owner' ? `<button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-rm="${esc(m.user_id)}">Remove from room</button>` : ''}</li>`;
  }).join('')}${invites.map(i => `<li><span class="grow">${esc(i.email)}</span>${pill('Invited', 'var(--status-attention)')}<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-cancel="${esc(i.invite_id)}">Cancel</button></li>`).join('')}</ul>`;
}

export function mountCollab(root, { fabricSessions = null, surface = root.dataset.standalone === '1' ? 'public' : 'dashboard' } = {}) {
  ensureConversationStyles(root.ownerDocument);
  const store = new TokenStore(window.sessionStorage);
  let operator = false; // per mount: credentials never carry over from another shell
  const api = new CollabApi(() => (operator ? { access: 'operator', id: 'operator' } : store.get()));
  let teardown = null;      // stops the open room's live loop
  let cursor = 0;
  const pendingAdministration = new Set();
  let lastCfg = null;
  let me = null;
  const standalone = surface === 'public';
  const linkedAgent = !standalone && /^#\/collab\?/.test(window.location.hash)
    ? new URLSearchParams(window.location.hash.split('?')[1]).get('agent') : null;
  let focusLinkedAgent = Boolean(linkedAgent);
  let current = linkedAgent ? '/agents' : '/rooms'; // the dashboard owns the URL hash
  const routePath = () => standalone ? window.location.hash : current;
  let generation = 0, disposed = false, navigationPath = routePath();
  const isCurrent = (turn) => !disposed && turn === generation && navigationPath === routePath();
  const cancelled = (error) => error?.name === 'AbortError';
  let routeState = collabRouteState({ surface, route: routePath() });
  const setStatus = (status, error) => { routeState = collabRouteState({ surface, route: routePath(), status, error }); };
  const nativeSessionLink = (id) => operator && !standalone && typeof id === 'string' && id
    ? `<a href="/#/herd/session/${esc(encodeURIComponent(id))}">Open agent conversation</a>` : '';
  const dispose = () => { if (teardown) teardown(); teardown = null; };
  const beginNavigation = () => {
    generation += 1; navigationPath = routePath();
    clearTimeout(waitTimer); dispose();
    return generation;
  };
  const go = (path, turn) => {
    if (!isCurrent(turn)) return;
    if (standalone && window.location.hash !== `#${path}`) { window.location.hash = path; beginNavigation(); }
    else { current = path; route(); }
  };
  const onHash = () => { if (!disposed) route(); };
  const $ = (id) => document.getElementById(id);

  async function signIn(cfg, turn) {
    if (!isCurrent(turn)) return;
    const { verifier, challenge } = await pkcePair();
    if (!isCurrent(turn)) return;
    const state = randomString(16);
    window.sessionStorage.setItem('hcn.pkce', JSON.stringify({ verifier, state }));
    window.location.assign(authorizeUrl(cfg, cfg.public_url, state, challenge));
  }

  const copy = async (text, btn, turn) => {
    if (!isCurrent(turn)) return;
    try {
      await navigator.clipboard.writeText(text);
      if (!isCurrent(turn)) return;
      btn.textContent = 'Copied';
    } catch (e) {
      if (!isCurrent(turn) || cancelled(e)) return;
      btn.textContent = 'Select and copy';
    }
    setTimeout(() => { if (isCurrent(turn)) btn.textContent = 'Copy'; }, 1800);
  };

  function frame(active, content, strip, turn) {
    if (!isCurrent(turn)) return;
    dispose();
    // Explicit navigation stays usable while another route is loading. Async
    // action completions instead pass their captured turn to go().
    const wire = () => root.querySelectorAll('[data-go]').forEach((b) => { b.onclick = () => go(b.dataset.go, generation); });
    if (!standalone) {
      root.innerHTML = `<div class="cx-embed">${strip}${content}</div>`;
      wire();
      return;
    }
    const link = (path, label) => `<a class="hd-shell__link${active === path ? ' active' : ''}" href="#${path}">${label}</a>`;
    root.innerHTML = `<div class="hd-shell">
      <aside class="hd-shell__sidebar">
        <div class="hd-shell__brand"><span class="cx-logo" aria-hidden="true">◆</span>HerdR Collaboration</div>
        <nav class="hd-shell__nav" aria-label="Collaboration">${link('/rooms', 'Rooms')}${link('/agents', 'AI agents')}${canManagePeople() ? link('/people', 'People') : ''}<a class="hd-shell__link" href="/guide" target="_blank" rel="noopener">Guide</a></nav>
        <div class="cx-user"><span>${esc(me ? (me.email || me.user_id) : '')}</span>${operator ? '' : '<button id="hcn-out" class="hd-btn hd-btn--ghost hd-btn--sm" type="button">Sign out</button>'}</div>
      </aside>
      <main class="hd-shell__main"><div class="hd-shell__content">${strip}${content}</div></main></div>`;
    wire();
    if ($('hcn-out')) $('hcn-out').onclick = () => { if (!isCurrent(turn)) return; store.clear(); window.location.hash = ''; start(); };
  }

  function centered(inner) {
    dispose();
    root.innerHTML = `<div class="cx-center"><div class="hd-card cx-signin"><div class="cx-logo" aria-hidden="true">◆</div>${inner}</div></div>`;
  }

  async function start() {
    if (disposed) return;
    const turn = beginNavigation();
    setStatus('loading');
    me = null;
    operator = false;
    let cfg;
    try { cfg = await loadConfig(); }
    catch (e) {
      if (!isCurrent(turn) || cancelled(e)) return;
      setStatus('degraded'); centered('<h1>Collaboration</h1><p role="alert">Unable to load collaboration configuration.</p>'); return;
    }
    if (!isCurrent(turn)) return;
    lastCfg = cfg;
    if (!cfg) {
      setStatus('degraded');
      centered('<h1>Collaboration</h1><p role="status">The collaboration network is not enabled on this server, or this address is not allowed for the dashboard. Try the dashboard by its IP address.</p>');
      return;
    }
    operator = Boolean(cfg.operator); // only the trusted operator listener supplies this identity
    if (!operator && window.location.origin !== new URL(cfg.public_url).origin) {
      setStatus('degraded');
      centered(`<h1>Collaboration</h1><p role="status">Collaboration is only available over HTTPS.</p>
        <a class="hd-btn" href="${esc(cfg.public_url)}">Open it here</a>`);
      return;
    }
    const err = window.sessionStorage.getItem('hcn.error');
    window.sessionStorage.removeItem('hcn.error');
    if (!operator && !store.get()) {
      setStatus('signed-out');
      centered(`<h1>HerdR Collaboration</h1>
        <p>Shared rooms for people and their AI agents. Sign in with your BinView account. New accounts wait for approval before anything is shared.</p>
        ${err ? `<p class="cx-msg" role="alert">${esc(err)}</p>` : ''}
        <button id="hcn-signin" class="hd-btn" type="button">Sign in</button>`);
      $('hcn-signin').onclick = () => signIn(cfg, turn);
      return;
    }
    try {
      if (!operator) await api.link(store.get().id); // record verified email and activate invites
      if (!isCurrent(turn)) return;
      const identity = await api.me();
      if (!isCurrent(turn)) return;
      me = identity;
      if (me.status !== 'approved') return approval(turn);
      await route();
    } catch (e) {
      if (!isCurrent(turn) || cancelled(e)) return;
      if (e instanceof AuthError && !operator) { store.clear(); return start(); }
      setStatus('error', e);
      centered(`<h1>Collaboration</h1><p class="cx-msg" role="alert">${esc(e.message)}</p>
        <p>If you were just added to the group, sign out and sign in again.</p>
        <button id="hcn-out2" class="hd-btn hd-btn--ghost" type="button">Sign out</button>`);
      $('hcn-out2').onclick = () => { if (!isCurrent(turn)) return; store.clear(); start(); };
    }
  }

  // Only pending approval is polled. Terminal decisions remain visible until sign-in/reload.
  let waitTimer = null;
  function approval(turn, error = null) {
    if (!isCurrent(turn)) return;
    clearTimeout(waitTimer);
    if (me.status !== 'pending') {
      const revoked = ['disabled', 'revoked', 'kicked'].includes(me.status);
      setStatus(revoked ? 'revoked' : 'forbidden');
      centered(`<h1>${revoked ? 'Access revoked' : 'Access denied'}</h1><p role="alert">Your collaboration account is ${esc(me.status)}.</p>
        <button id="hcn-out3" class="hd-btn hd-btn--ghost" type="button">Sign out</button>`);
      $('hcn-out3').onclick = () => { if (!isCurrent(turn)) return; store.clear(); start(); };
      return;
    }
    setStatus(error ? 'degraded' : 'waiting');
    if (error) centered(`<h1>Approval check unavailable</h1><p role="alert">${esc(error.message)}</p><p>Retrying shortly.</p>
      <button id="hcn-out3" class="hd-btn hd-btn--ghost" type="button">Sign out</button>`);
    else
    centered(`<h1>Waiting for approval</h1>
      <p>You're signed in as <strong>${esc(me.email || me.user_id)}</strong>. The person who runs this network, or a lab admin, has to approve your account before you can use rooms or connect agents. Nothing is shared with you until then.</p>
      <p class="cx-sub">This page checks every few seconds and opens as soon as you're approved.</p>
      <button id="hcn-out3" class="hd-btn hd-btn--ghost" type="button">Sign out</button>`);
    $('hcn-out3').onclick = () => { if (!isCurrent(turn)) return; clearTimeout(waitTimer); store.clear(); start(); };
    pollApproval(turn);
  }

  function pollApproval(turn) {
    if (!isCurrent(turn)) return;
    waitTimer = setTimeout(async () => {
      if (!isCurrent(turn)) return;
      try {
        const now = await api.me();
        if (!isCurrent(turn)) return;
        me = now;
        if (me.status === 'approved') return route();
        return approval(turn);
      } catch (e) {
        if (!isCurrent(turn)) return;
        if (cancelled(e)) { pollApproval(turn); return; }
        if (e instanceof AuthError) {
          if (!operator) { store.clear(); return start(); }
          setStatus('signed-out'); centered('<h1>Collaboration</h1><p role="alert">Session expired.</p>'); return;
        }
        return approval(turn, e);
      }
    }, 5000);
  }

  async function route() {
    if (disposed) return;
    const turn = beginNavigation();
    if (!me) return start();
    if (me.status !== 'approved') return approval(turn);
    setStatus('loading');
    if (routeState.status === 'not-found') {
      centered('<h1>Collaboration</h1><p role="alert">Room not found.</p>');
      return;
    }
    try {
      if (routeState.roomId) await showRoom(routeState.roomId, turn);
      else if (routeState.route === '/agents') await showAgents(turn);
      else if (routeState.route === '/people' && canManagePeople()) await showPeople('pending', false, turn);
      else {
        if (routeState.route === '/people') { setStatus('forbidden'); centered('<h1>Collaboration</h1><p role="alert">Access denied.</p>'); return; }
        await showRooms(turn);
      }
      if (!isCurrent(turn)) return;
      setStatus('ready');
    } catch (e) {
      if (!isCurrent(turn) || cancelled(e)) return;
      if (e instanceof AuthError && !operator) { store.clear(); return start(); }
      setStatus('error', e);
      centered(`<h1>Collaboration</h1><p role="alert">${esc(e.message)}</p>`);
    }
  }

  async function showRooms(turn) {
    const { rooms } = await api.rooms();
    if (!isCurrent(turn)) return;
    const strip = await tabsStrip(null, '/rooms', turn);
    if (!isCurrent(turn)) return;
    frame('/rooms', `<div class="cx-head"><div><h1>Rooms</h1><p class="cx-sub">Conversations you and your agents can take part in.</p></div></div>
      ${rooms.length ? `<div class="hd-grid" id="hcn-rooms">${rooms.map((r) => `<button type="button" class="hd-card cx-room" data-room="${esc(r.room_id)}">
          <span class="hd-card__title">${esc(r.name)}</span>
          <span class="cx-row cx-row--between">${pill(roleLabel(r.role), r.role === 'owner' ? 'var(--accent)' : 'var(--status-active)')}<span class="hd-card__sub">Open →</span></span></button>`).join('')}</div>`
        : '<div class="hd-empty">No rooms yet. Create your first one below.</div>'}
      ${operator ? '<div id="hcn-helpers-panel"></div>' : ''}
      <section class="cx-section"><h2>New room</h2>
        <form id="hcn-new" class="hd-card cx-form"><label class="cx-field">Room name<input name="name" required maxlength="120" placeholder="e.g. Metabolomics review"></label>
          <button class="hd-btn" type="submit">Create room</button><span id="hcn-new-msg" class="cx-msg" role="status"></span></form></section>`, strip, turn);
    document.querySelectorAll('#hcn-rooms [data-room]').forEach((b) => { b.onclick = () => { go(`/room/${encodeURIComponent(b.dataset.room)}`, generation); }; });
    if (operator && $('hcn-helpers-panel')) {   // all rooms' host requests, helpers and the panic switch; the operator is rarely a member of the rooms asking
      const panel = createHelpersPanel({ container: $('hcn-helpers-panel'), api, operator, getRooms: async () => (await api.rooms()).rooms || [], go: (path) => go(path, turn), hidden: () => document.hidden });
      teardown = () => panel.destroy();
      panel.start();
    }
    $('hcn-new').onsubmit = async (ev) => {
      ev.preventDefault();
      if (!isCurrent(turn)) return;
      const name = new FormData(ev.target).get('name');
      try {
        const p = await api.createProject(name);
        if (!isCurrent(turn)) return;
        const r = await api.createRoom(p.project_id, name);
        go(`/room/${encodeURIComponent(r.room_id)}`, turn);
      } catch (e) { if (!isCurrent(turn) || cancelled(e)) return; $('hcn-new-msg').textContent = e.message; $('hcn-new-msg').setAttribute('role', 'alert'); }
    };
  }

  const canManagePeople = () => Boolean(me?.capabilities.canManagePeople);

  async function showPeople(filter, showLog, turn) {
    if (!isCurrent(turn)) return;
    let data;
    try { data = await api.users(); } catch (e) { if (!cancelled(e)) go('/rooms', turn); return; }
    if (!isCurrent(turn)) return;
    const manage = Boolean(data.can_manage);
    const strip = await tabsStrip(null, '/people', turn);
    if (!isCurrent(turn)) return;
    frame('/people', renderPeople({ users: data.users, manage, filter, showLog, counts: data.counts }), strip, turn);
    document.querySelectorAll('[data-filter]').forEach((b) => { b.onclick = () => showPeople(b.dataset.filter, showLog, turn); });
    document.querySelectorAll('[data-act]').forEach((b) => {
      b.onclick = async () => {
        if (!isCurrent(turn)) return;
        const act = b.dataset.act;
        const key = `person:${b.dataset.user}`;
        if (pendingAdministration.has(key)) return;
        const who = b.closest('li').querySelector('b').textContent;
        const ask = { disable: `Disable ${who}? They and their agents are locked out until you enable them.`, deny: `Decline ${who}?` }[act];
        if (act === 'delete' && !confirmDeletePerson({ email: who })) return;
        if (ask && !window.confirm(ask)) return;
        pendingAdministration.add(key);
        b.disabled = true;
        const msg = $('hcn-people-msg');
        msg.setAttribute('role', 'status');
        if (act === 'delete') msg.textContent = 'Deleting person and scrubbing retained archives…';
        try {
          if (act === 'delete') {
            const result = await api.deletePerson(b.dataset.user);
            if (!isCurrent(turn)) return;
            if (result.status !== 'complete') throw new Error('Person deletion is not complete. Refresh People before retrying.');
          } else await api.decideUser(b.dataset.user, act, ($('hcn-user-note').value || '').trim());
          await showPeople(filter, showLog, turn);
          if (isCurrent(turn) && act === 'delete') $('hcn-people-msg').textContent = 'Person deleted permanently. Room-owned files remain.';
        }
        catch (e) { if (!isCurrent(turn) || cancelled(e)) return; $('hcn-people-msg').textContent = e.message; $('hcn-people-msg').setAttribute('role', 'alert'); }
        finally { pendingAdministration.delete(key); if (isCurrent(turn)) b.disabled = false; }
      };
    });
    if (manage) {
      const paint = async () => {
        const box = $('hcn-log');
        if (!showLog) { box.innerHTML = ''; return; }
        try {
          const { log } = await api.userLog();
          if (!isCurrent(turn)) return;
          box.innerHTML = log.length ? `<table class="cx-table"><thead><tr><th>When</th><th>Who</th><th>What</th><th>By</th><th>Note</th></tr></thead><tbody>${log.map((r) => `<tr><td>${esc(when(r.at))}</td><td>${esc(r.email || shortId(r.user_id))}</td><td>${esc(String(r.action || '').replace('user.', ''))}</td><td>${esc(r.by || 'sign-in')}</td><td>${esc(r.reason || '')}</td></tr>`).join('')}</tbody></table>` : '<div class="hd-empty">Nothing yet.</div>';
        } catch (e) { if (isCurrent(turn) && !cancelled(e)) box.textContent = e.message; }
      };
      $('hcn-log-btn').onclick = () => showPeople(filter, !showLog, turn);
      paint();
    }
  }

  // Collaboration handles route mentions; canonical dashboard names are edited in Agents.
  function wireRename(box, after, turn) {
    box.querySelectorAll('[data-rename]').forEach((btn) => {
      btn.onclick = () => {
        if (!isCurrent(turn)) return;
        const holder = btn.closest('.cx-ntitle');
        const id = btn.dataset.rename;
        const old = btn.dataset.name;
        holder.innerHTML = `<form class="cx-rename"><span class="cx-at">@</span><input class="cx-input" name="n" value="${esc(old)}" maxlength="32" pattern="[a-z0-9]+(-[a-z0-9]+)*" required aria-label="New @ handle" autocomplete="off"><button class="hd-btn hd-btn--sm" type="submit">Save handle</button><button class="hd-btn hd-btn--ghost hd-btn--sm" type="button" data-x="1">Cancel</button><span class="cx-msg" role="alert"></span></form>`;
        const f = holder.querySelector('form');
        const input = f.elements.n;
        input.focus();
        input.select();
        f.querySelector('[data-x]').onclick = () => { if (isCurrent(turn)) after(); };
        f.onsubmit = async (ev) => {
          ev.preventDefault();
          if (!isCurrent(turn)) return;
          const next = input.value.trim().toLowerCase();
          if (next === old) { after(); return; }
          try { await api.renameAgent(id, next); if (isCurrent(turn)) after(); } catch (e) { if (isCurrent(turn) && !cancelled(e)) f.querySelector('.cx-msg').textContent = e.message; }
        };
        f.onkeydown = (ev) => { if (ev.key === 'Escape' && isCurrent(turn)) after(); };
      };
    });
  }

  async function showAgents(turn) {
    const strip = await tabsStrip(null, '/agents', turn);
    if (!isCurrent(turn)) return;
    frame('/agents', `<div class="cx-head"><div><h1>AI agents</h1><p class="cx-sub">Connect Claude Code, Codex, Pi or any MCP client so it can work in your rooms.</p></div>
        <div class="cx-row"><input id="hcn-agent-name" class="cx-input" maxlength="32" pattern="[a-z0-9]+(-[a-z0-9]+)*" placeholder="name (optional)" aria-label="Agent name" autocomplete="off"><button id="hcn-connect" class="hd-btn" type="button">Connect an AI agent</button></div></div>
      <div id="hcn-connect-out"></div>
      <section class="cx-section"><h2>Add a fleet of agents</h2><div id="hcn-fleet"></div><div id="hcn-fleet-out"></div><div id="hcn-spawns"></div></section>
      ${operator ? '<section class="cx-section"><h2>Hosts</h2><p class="cx-sub">Fleets started from here are spread over these hosts in turn. Untick a host to keep automatic agents off it.</p><div id="hcn-hosts"></div></section>' : ''}
      <section class="cx-section"><h2>Your agents</h2><div id="hcn-agents"></div></section>
      <section class="cx-section">${GUIDE}</section>`, strip, turn);
    await renderAgents(turn);
    if (!isCurrent(turn)) return;
    await renderFleet(turn);
    if (!isCurrent(turn)) return;
    if (operator) {            // keep the host list and fleet progress fresh while this page is open
      let alive = true;
      let tick = null;
      const cycle = async () => {
        await renderHosts(turn);
        if (!isCurrent(turn)) return;
        const busy = await renderSpawns(turn);
        if (!isCurrent(turn)) return;
        if (busy) await renderAgents(turn);
        if (!isCurrent(turn)) return;
        if (alive && !document.hidden) tick = setTimeout(cycle, busy ? 2000 : 6000);
        else if (alive) tick = setTimeout(cycle, 6000);
      };
      teardown = () => { alive = false; clearTimeout(tick); };
      cycle();
    }
    $('hcn-connect').onclick = async () => {
      if (!isCurrent(turn)) return;
      const btn = $('hcn-connect');
      btn.disabled = true;
      try {
        const credential = await api.connectAgent($('hcn-agent-name').value.trim().toLowerCase());
        if (!isCurrent(turn)) return;
        showCredential(credential, turn);
        await renderAgents(turn);
      } catch (e) {
        if (!isCurrent(turn) || cancelled(e)) return;
        $('hcn-connect-out').innerHTML = `<div class="hd-error" role="alert">${esc(e.message)}</div>`;
      } finally { if (isCurrent(turn)) btn.disabled = false; }
    };
  }

  // ---- fleets: many agents of one family, registered by copy-paste scripts
  const FAMILIES = ['claude-code', 'codex', 'pi', 'agent'];
  async function renderFleet(turn) {
    if (!isCurrent(turn)) return;
    const box = $('hcn-fleet');
    if (!box) return;
    const { rooms } = await api.rooms();
    if (!isCurrent(turn)) return;
    const usable = rooms.filter((r) => ['owner', 'contributor'].includes(r.role));
    let parents = [];
    try { parents = (await api.agents()).agents.filter((a) => a.status === 'active' && /^pane:/.test(a.instance_id || '')); } catch { /* the form works without */ }
    if (!isCurrent(turn)) return;
    box.innerHTML = `<form id="hcn-fleet-form" class="cx-stack">
        <div class="cx-row">
          <label class="cx-msg">Family <select name="family">${FAMILIES.map((f) => `<option>${esc(f)}</option>`).join('')}</select></label>
          <label class="cx-msg">Name prefix <input class="cx-input" name="prefix" maxlength="24" pattern="[a-z0-9]+(-[a-z0-9]+)*" value="worker" required></label>
          <label class="cx-msg">How many <input class="cx-input" name="count" type="number" min="1" max="50" value="5" required></label>
          <label class="cx-msg">Room <select name="room"><option value="">none yet</option>${usable.map((r) => `<option value="${esc(r.room_id)}">${esc(r.name)}</option>`).join('')}</select></label>
          <label class="cx-msg">Started under <select name="parent"><option value="">no one (a tab of their own)</option>${parents.map((a) => `<option value="${esc(a.agent_id)}">@${esc(a.name)} (its tab)</option>`).join('')}</select></label>
        </div>
        <span class="cx-checks">
          <label class="cx-check"><input type="checkbox" name="run" checked> Start the workers right away</label>
          <label class="cx-check"><input type="checkbox" name="inherdr" checked> Run in herdr: workspace <code>herd-PREFIX</code>, a tab per agent</label>
          <label class="cx-check"><input type="checkbox" name="tools"> Let them use tools (shell, files) on task text</label></span>
        <span class="cx-checks">${permBoxes(['read', 'post', 'execute', 'instruct'], 'fp')}</span>
        ${operator ? `<label class="cx-check"><input type="radio" name="how" value="spawn" checked> Start them on my hosts (herdr, round robin): they keep running</label>
        <label class="cx-check"><input type="radio" name="how" value="direct"> From this dashboard (tailnet): create now, no token</label>
        <label class="cx-check"><input type="radio" name="how" value="token"> Copy-paste script for any machine (join token)</label>` : '<input type="hidden" name="how" value="token">'}
        <div><button class="hd-btn" type="submit">Create</button> <span id="hcn-fleet-msg" class="cx-msg" role="status"></span></div>
        <p class="cx-msg">Agents start with no access. The room owner approves them (one click for the whole family under <em>Agent requests</em>), and every permission can lapse.</p>
      </form>`;
    $('hcn-fleet-form').onsubmit = async (ev) => {
      ev.preventDefault();
      if (!isCurrent(turn)) return;
      const f = ev.target;
      const fd = new FormData(f);
      const room = fd.get('room');
      const parent = fd.get('parent');
      const body = { harness_type: fd.get('family'), name_prefix: String(fd.get('prefix')).toLowerCase(), count: Number(fd.get('count')),
        ...(room ? { room_id: room, actions: picked(f, 'fp') } : {}), ...(parent ? { parent_agent_id: parent } : {}) };
      const flags = [fd.get('run') || fd.get('inherdr') ? '--start' : '', fd.get('inherdr') ? '--herdr' : '', fd.get('tools') ? '--tools' : ''].filter(Boolean);
      const msg = $('hcn-fleet-msg');
      msg.textContent = '';
      try {
        if (fd.get('how') === 'spawn') {
          await api.requestSpawn({ harness_type: body.harness_type, name_prefix: body.name_prefix, count: body.count, ...(room ? { room_id: room, actions: body.actions } : {}), ...(parent ? { parent_agent_id: parent } : {}) });
          if (!isCurrent(turn)) return;
          $('hcn-fleet-out').innerHTML = '';
          msg.textContent = 'Requested. They start within a few seconds; progress is listed below.';
          await renderSpawns(turn);
          return;
        }
        if (fd.get('how') === 'direct') showFleetFiles(await api.bulkConnect(body), body, turn);
        else showFleetScript(await api.createJoinToken(body), body, flags, turn);
        await renderAgents(turn);
        await renderJoinTokens(turn);
      } catch (e) { if (!isCurrent(turn) || cancelled(e)) return; msg.textContent = e.message; msg.setAttribute('role', 'alert'); }
    };
    await renderJoinTokens(turn);
  }

  // ---- hosts that may receive automatic agents, and the progress of fleets started on them
  async function renderHosts(turn) {
    if (!isCurrent(turn)) return;
    const box = $('hcn-hosts');
    if (!box || !operator) return;
    let hosts;
    try { hosts = (await api.fleetHosts()).hosts; } catch { return; }
    if (!isCurrent(turn)) return;
    const html = hosts.length ? `<ul class="cx-list">${hosts.map((h) => `<li><span class="grow"><b>${esc(h.host)}</b> ${pill(h.online ? 'online' : 'offline', h.online ? 'var(--status-ok)' : 'var(--text-muted)')}${h.kind === 'local' ? ' <span class="hd-ident">this machine</span>' : ''}${h.online ? ` <span class="hd-ident">${h.families && h.families.length ? esc(h.families.join(', ')) : 'no agent CLIs found'}</span>` : ''}</span><label class="cx-check"><input type="checkbox" data-auto="${esc(h.host)}"${h.auto_agents ? ' checked' : ''}> Automatic agents</label></li>`).join('')}</ul>` : '<div class="hd-empty">No hosts reported yet. The host runner reports every few seconds.</div>';
    if (box.dataset.h === html) return;
    box.dataset.h = html; box.innerHTML = html;
    box.querySelectorAll('[data-auto]').forEach((c) => { c.onchange = async () => {
      if (!isCurrent(turn)) return;
      try { await api.setFleetHost(c.dataset.auto, c.checked); await renderHosts(turn); }
      catch (e) { if (isCurrent(turn) && !cancelled(e)) alert(e.message); }
    }; });
  }
  async function renderSpawns(turn) {
    if (!isCurrent(turn)) return false;
    const box = $('hcn-spawns');
    if (!box || !operator) return false;
    let list;
    try { list = (await api.spawns()).spawns.slice(0, 8); } catch { return false; }
    if (!isCurrent(turn)) return false;
    const busy = list.some((s) => ['requested', 'claimed'].includes(s.status));
    const colour = { requested: 'var(--status-attention)', claimed: 'var(--accent)', done: 'var(--status-ok)', stalled: 'var(--status-failed)', expired: 'var(--text-muted)' };
    const html = list.length ? `<h3>Recent fleet starts</h3><ul class="cx-list">${list.map((s) => `<li><span class="grow"><b>${esc(s.name_prefix)}-N</b> ${esc(s.harness_type)} × ${s.count}${s.error ? `<br><span class="cx-msg" role="alert">${esc(s.error)}</span>` : ''}${s.placements.length ? `<br>${s.placements.map((p) => `<span class="hd-ident" title="${esc(p.error || '')}">${esc(p.name)} → ${esc(p.host)}${p.status === 'failed' ? ' ✗' : ''}</span>`).join(' ')}` : ''}</span>${pill(s.status === 'done' && s.placements.some((p) => p.status === 'failed') ? 'partly started' : s.status, colour[s.status] || 'var(--text-muted)')}</li>`).join('')}</ul>` : '';
    if (box.dataset.h !== html) { box.dataset.h = html; box.innerHTML = html; }
    return busy;
  }

  const scriptCard = (title, text, note) => `<div class="hd-card cx-stack"><div class="hd-card__title">${esc(title)}</div>
      <div class="cx-code"><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-copy>Copy</button><pre data-text></pre></div>
      <p class="cx-msg">${esc(note)}</p></div>`;
  function fillScripts(out, items, turn) {
    if (!isCurrent(turn)) return;
    out.innerHTML = items.map((i) => scriptCard(i.title, i.text, i.note)).join('');
    out.querySelectorAll('.hd-card').forEach((card, n) => {
      card.querySelector('[data-text]').textContent = items[n].text;     // textContent: never parsed as HTML
      const btn = card.querySelector('[data-copy]');
      btn.onclick = () => copy(items[n].text, btn, turn);
    });
  }

  function showFleetScript(tok, body, flags, turn) {
    if (!isCurrent(turn)) return;
    const base = lastCfg.public_url;
    const opts = { base, token: tok.token, count: tok.max_uses, prefix: tok.name_prefix };
    const oneLiner = `curl -fsSL ${base.replace(/\/$/, '')}/api/v1/collab/scripts/join.sh | HERDR_JOIN_TOKEN=${shq(tok.token)} bash -s -- --count ${tok.max_uses}${flags.length ? ` ${flags.join(' ')}` : ''}`;
    fillScripts($('hcn-fleet-out'), [
      { title: `1. One line for each machine: registers ${tok.max_uses} ${tok.harness_type} agent${tok.max_uses === 1 ? '' : 's'}${flags.includes('--start') ? ' and starts them' : ''}`, text: oneLiner,
        note: `Downloads the scripts from this site, registers the agents${body.room_id ? ' and asks for access to the room' : ''}${flags.includes('--herdr') ? `, and lays them out in herdr: workspace herd-${tok.name_prefix}, one tab per agent${body.parent_agent_id ? ', as splits of the parent agent\'s tab' : ''}` : ''}. The token works ${tok.max_uses} time${tok.max_uses === 1 ? '' : 's'} until ${new Date(tok.expires_at * 1000).toLocaleString()}. Later: replace the options with --status or --stop. Treat it like a password.` },
      { title: 'Offline version (the same, as one pasted script)', text: bundleScript(opts),
        note: `The token works for ${tok.max_uses} agents until ${new Date(tok.expires_at * 1000).toLocaleString()}, then stops. It only creates agents with no access${body.room_id ? ' and files a request in the room' : ''}. Treat it like a password.` },
      { title: '2. Make an agent do work', text: `HERDR_AGENT_CMD='claude -p' ~/herdr-agents/worker.sh ~/herdr-agents/${tok.name_prefix}-1.json`,
        note: 'Waits for tasks, hands each task text to your command on stdin and reports its output. Without HERDR_AGENT_CMD it just acknowledges.' },
      { title: '3. Let an agent delegate', text: `~/herdr-agents/agents.sh ~/herdr-agents/${tok.name_prefix}-1.json ROOM_ID\n~/herdr-agents/delegate.sh ~/herdr-agents/${tok.name_prefix}-1.json ROOM_ID TARGET_AGENT_ID PARENT_TASK_ID "what to do"`,
        note: 'An agent can only pass on part of a task it accepted, so work always traces back to a person.' },
    ], turn);
  }

  function showFleetFiles(res, body, turn) {
    if (!isCurrent(turn)) return;
    const files = filesFromBulk(lastCfg.public_url, res.agents);
    const out = $('hcn-fleet-out');
    fillScripts(out, [
      { title: `Created ${res.agents.length} agents${body.room_id ? ' and added them to the room' : ''}`,
        text: `mkdir -p ~/herdr-agents && cd ~/herdr-agents\n${files.map((f) => `cat > ${f.name} <<'EOF'\n${f.text}EOF`).join('\n')}\nchmod 600 *.json`,
        note: 'These credentials are shown once. Paste on the machine that runs the agents, then use worker.sh below.' },
      { title: 'worker.sh', text: workerScript({ base: lastCfg.public_url }), note: 'Save as ~/herdr-agents/worker.sh and chmod +x. Run: worker.sh AGENT.json' },
    ], turn);
  }

  async function renderJoinTokens(turn) {
    if (!isCurrent(turn)) return;
    const box = $('hcn-fleet-out');
    if (!box) return;
    let list;
    try { list = (await api.joinTokens()).tokens.filter((t) => t.status === 'active'); } catch { return; }
    if (!isCurrent(turn)) return;
    let holder = $('hcn-tokens');
    if (!holder) { holder = document.createElement('div'); holder.id = 'hcn-tokens'; box.after(holder); }
    holder.innerHTML = list.length ? `<h3>Active join tokens</h3><ul class="cx-list">${list.map((t) => `<li><span class="grow"><b>${esc(t.name_prefix)}-N</b> ${esc(t.harness_type)} · ${t.uses}/${t.max_uses} used · until ${esc(new Date(t.expires_at * 1000).toLocaleString())}</span><button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-revoke-token="${esc(t.join_id)}">Revoke</button></li>`).join('')}</ul>` : '';
    holder.querySelectorAll('[data-revoke-token]').forEach((b) => { b.onclick = async () => {
      if (!isCurrent(turn)) return;
      try { await api.revokeJoinToken(b.dataset.revokeToken); await renderJoinTokens(turn); }
      catch (e) { if (isCurrent(turn) && !cancelled(e)) alert(e.message); }
    }; });
  }

  function showCredential(a, turn) {
    if (!isCurrent(turn)) return;
    const snippets = connectSnippets(lastCfg, a);
    const out = $('hcn-connect-out');
    out.innerHTML = `<div class="hd-card cx-stack">
      <div class="cx-warn"><strong>Copy this now.</strong> The credential is shown only once and acts as you in the rooms you add this agent to. Keep it private.</div>
      <div class="cx-tabs" role="tablist">${snippets.map((s, i) => `<button class="cx-tab" role="tab" type="button" data-tab="${i}" aria-selected="${i === 0}">${esc(s.name)}</button>`).join('')}</div>
      <div class="cx-code"><button id="hcn-copy" class="hd-btn hd-btn--ghost hd-btn--sm" type="button">Copy</button><pre id="hcn-snippet"></pre></div>
      <p id="hcn-note" class="cx-msg"></p>
      <p class="cx-msg">Next: open a room and add this agent under <em>Your agents in this room</em>.</p></div>`;
    const pick = (i) => {
      if (!isCurrent(turn)) return;
      $('hcn-snippet').textContent = snippets[i].text;   // textContent: never parsed as HTML
      $('hcn-note').textContent = snippets[i].note;
      out.querySelectorAll('[data-tab]').forEach((t) => t.setAttribute('aria-selected', String(Number(t.dataset.tab) === i)));
      $('hcn-copy').onclick = () => copy(snippets[i].text, $('hcn-copy'), turn);
    };
    out.querySelectorAll('[data-tab]').forEach((t) => { t.onclick = () => pick(Number(t.dataset.tab)); });
    pick(0);
  }

  async function renderAgents(turn) {
    if (!isCurrent(turn)) return;
    const box = $('hcn-agents');
    if (!box) return;
    const { agents } = await api.agents();
    if (!isCurrent(turn)) return;
    const live = agents.filter((a) => a.status === 'active');
    // On the dashboard the owner can tie an agent to a running fabric session (the join key is the session id).
    let sessions = null;
    if (operator && fabricSessions) { try { const r = await fabricSessions(); sessions = Array.isArray(r) ? r : (r.sessions || []); } catch { sessions = null; } }
    if (!isCurrent(turn)) return;
    const sessionOptions = (cur) => {
      const list = [...(sessions || [])];
      if (cur && !list.some((x) => x.session_id === cur)) list.unshift({ session_id: cur, agent_role: 'linked', host_id: '' });
      return list.map((x) => `<option value="${esc(x.session_id)}"${x.session_id === cur ? ' selected' : ''}>${esc(`${x.display_name || x.agent_role || 'agent'} · ${x.host_id || ''} · ${String(x.cwd || x.session_id).split('/').filter(Boolean).pop()}`)}</option>`).join('');
    };
    const knownSessions = new Set((sessions || []).map((session) => session.session_id));
    box.innerHTML = live.length ? `<div class="hd-grid">${live.map((a) => `<div class="hd-card cx-stack cx-agentcard" data-agent-card="${esc(a.agent_id)}" tabindex="-1"${a.agent_id === linkedAgent ? ' style="outline:2px solid var(--accent);outline-offset:2px"' : ''}>
        <div class="cx-row cx-row--between"><span class="hd-card__title">${agentTitle({ ...a, name: a.name || a.harness_type, display_name: sessions?.find(s=>s.session_id===a.instance_id)?.display_name }, true)}</span>${pill('Active', 'var(--status-ok)')}</div>
        <span class="hd-ident">${esc(a.harness_type)} · ${esc(a.agent_id)}</span>
        ${a.instance_id ? '<span class="hd-card__sub">Linked to a HerdR agent. The readable name is shared; the @ handle routes Collaboration tasks.</span>' : ''}
        ${knownSessions.has(a.instance_id) ? nativeSessionLink(a.instance_id) : ''}
        <span class="hd-card__sub">Expires ${esc(new Date(a.expires_at * 1000).toLocaleDateString())} · ${a.rooms.length ? `in ${esc(a.rooms.map((r) => r.name).join(', '))}` : 'not in any room yet'}</span>
        ${sessions ? `<label class="cx-msg">Fabric agent <select data-link="${esc(a.agent_id)}"><option value="">not linked</option>${sessionOptions(a.instance_id)}</select></label>` : ''}
        <div><button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-revoke="${esc(a.agent_id)}">Revoke</button></div></div>`).join('')}</div>`
      : '<div class="hd-empty">No connected agents yet.</div>';
    if (focusLinkedAgent) {
      const selected = [...box.querySelectorAll('[data-agent-card]')].find((card) => card.dataset.agentCard === linkedAgent);
      if (selected) { selected.focus({ preventScroll: true }); selected.scrollIntoView({ block: 'center' }); focusLinkedAgent = false; }
    }
    wireRename(box, () => renderAgents(turn), turn);
    box.querySelectorAll('[data-link]').forEach((sel) => { sel.onchange = async () => {
      if (!isCurrent(turn)) return;
      try { await api.linkAgent(sel.dataset.link, sel.value); } catch (e) { if (isCurrent(turn) && !cancelled(e)) alert(e.message); }
    }; });
    box.querySelectorAll('[data-revoke]').forEach((b) => { b.onclick = async () => {
      if (!isCurrent(turn)) return;
      try { await api.revokeAgent(b.dataset.revoke); await renderAgents(turn); }
      catch (e) { if (isCurrent(turn) && !cancelled(e)) alert(e.message); }
    }; });
  }

  // ---- one tab per room, then the AI agents page
  let roomsCache = [];
  async function tabsStrip(activeRoom, activePage, turn) {
    try {
      const { rooms } = await api.rooms();
      if (!isCurrent(turn)) return '';
      roomsCache = rooms;
    } catch (e) { if (cancelled(e)) throw e; /* keep the last list */ }
    if (!isCurrent(turn)) return '';
    let waitingCount = 0;
    if (canManagePeople()) { try { waitingCount = (await api.users('pending')).users.length; } catch { /* not an approver */ } }
    if (!isCurrent(turn)) return '';
    const t = (to, label, on, extra = '') => `<button type="button" class="cx-rtab${extra}" role="tab" data-go="${to}" aria-selected="${on}">${label}</button>`;
    return `<div class="cx-rtabs" role="tablist" aria-label="Rooms">${roomsCache.map((r) => t(`/room/${encodeURIComponent(r.room_id)}`, esc(r.name), r.room_id === activeRoom)).join('')}${t('/rooms', '＋ New room', activePage === '/rooms', ' cx-rtab--new')}<span class="grow"></span>${canManagePeople() ? t('/people', `People${waitingCount ? ` <span class="cx-badge" title="waiting for approval">${waitingCount}</span>` : ''}`, activePage === '/people') : ''}${t('/agents', 'AI agents', activePage === '/agents')}</div>`;
  }

  const hueOf = (s) => { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
  const initials = (s) => (String(s).replace(/^@/, '').match(/[a-z0-9]/gi) || ['?']).slice(0, 2).join('').toUpperCase();
  const subTab = new Map();                // room id -> the sub-tab last open there

  async function showRoom(roomId, turn) {
    cursor = 0;
    const info = await api.room(roomId);
    if (!isCurrent(turn)) return;
    const strip = await tabsStrip(roomId, null, turn);
    if (!isCurrent(turn)) return;
    const { canInvite, canPost } = info.capabilities;
    const guest = info.role === 'guest';
    let names = new Map((info.members || []).map((m) => [m.user_id, m.email || shortId(m.user_id)]));
    const agentName = new Map();           // agent id -> handle, for rendering task events
    const S = { agents: [], tasks: [], flow: null, requests: null, mine: [], profile: null, allow: [], egress: [], helpers: [], paused: false, entries: null };
    const tabs = [['chat', 'Chat'], ...(guest ? [] : [['flow', 'Flow']]), ['tasks', 'Tasks'], ...(guest ? [] : [['agents', 'Agents']]), ...(canInvite ? [['people', 'People']] : [])];
    let sub = subTab.get(roomId);
    if (!tabs.some(([k]) => k === sub)) sub = 'chat';

    frame('/rooms', `<div class="cx-roomhead"><h1>${esc(info.name)}</h1>${pill(roleLabel(info.role), 'var(--accent)')}</div>
      <div class="cx-stabs" role="tablist" aria-label="${esc(info.name)}">${tabs.map(([k, l]) => `<button type="button" class="cx-stab" role="tab" data-sub="${k}" aria-selected="${k === sub}">${l}<span class="cx-badge" id="hcn-b-${k}" hidden></span></button>`).join('')}</div>
      <section class="hd-card cx-chat cx-panel" id="hcn-p-chat" aria-label="Conversation">
        <div id="hcn-log" class="cx-log" role="log" aria-live="polite"><div class="hd-empty" id="hcn-empty">No messages yet.</div></div>
        <button type="button" id="hcn-jump" class="hd-btn hd-btn--sm cx-jump" hidden>↓ New messages</button>
        ${canPost ? '<div id="hcn-composer"></div>'
        : '<div class="cx-msg" style="padding:12px">Read-only access.</div>'}
      </section>
      ${guest ? '' : `<section class="hd-card cx-stack cx-panel" id="hcn-p-flow" hidden aria-label="Flow of work"><div class="cx-row cx-row--between"><div class="hd-card__title">Flow of work</div><span class="cx-msg">People and agents; lines are tasks, moving lines are work in progress.</span></div><div id="hcn-flow"></div><div id="hcn-flow-detail" class="cx-msg" role="status"></div></section>`}
      <section class="cx-panel cx-cols" id="hcn-p-tasks" hidden>
        ${canPost ? '<div class="hd-card cx-stack"><div class="hd-card__title">Give a task</div><form id="hcn-task" class="cx-stack"><label class="cx-field">To<select name="target" id="hcn-task-target"></select></label><label class="cx-field">What should it do?<textarea class="cx-input" name="text" required maxlength="4000" rows="4"></textarea></label><button class="hd-btn" type="submit">Send task</button><span id="hcn-task-msg" class="cx-msg" role="alert"></span></form></div>' : ''}
        <div class="hd-card cx-stack"><div class="hd-card__title">Tasks</div><div id="hcn-tasks"></div></div></section>
      ${guest ? '' : `<section class="cx-panel cx-cols" id="hcn-p-agents" hidden>
        <div class="hd-card cx-stack"><div class="hd-card__title">Agents in this room</div><div id="hcn-roomagents"></div></div>
        ${canInvite ? '<div class="hd-card cx-stack"><div class="hd-card__title">Agent requests</div><div id="hcn-requests"></div></div>' : ''}
        ${canPost ? '<div class="hd-card cx-stack"><div class="hd-card__title">Your agents</div><div id="hcn-grant"></div><span id="hcn-agent-msg" class="cx-msg" role="status"></span></div>' : ''}
        ${operator ? '<div class="hd-card cx-stack"><div class="hd-card__title">Helpers</div><div id="hcn-panic"></div><div class="hd-card__title">Host access requests</div><div id="hcn-egress"></div><div class="hd-card__title">External hosts</div><div id="hcn-hosts-table"></div></div>' : ''}
        ${canInvite ? '<div class="hd-card cx-stack"><div class="hd-card__title">Helper profile</div><div id="hcn-profile-box"></div></div>' : ''}</section>`}
      ${canInvite ? `<section class="cx-panel cx-cols" id="hcn-p-people" hidden><div class="hd-card cx-stack"><div class="hd-card__title">People</div><div id="hcn-people"></div><span id="hcn-members-msg" class="cx-msg" role="status"></span></div>
        <div class="hd-card cx-stack"><div class="hd-card__title">Invite</div><form id="hcn-invite" class="cx-stack"><label class="cx-field">Invite by email<input name="email" type="email" required placeholder="name@example.org"></label>
          <label class="cx-field">Access<select name="role">${ROLES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}</select></label>
          <button class="hd-btn" type="submit">Send invite</button><span id="hcn-invite-msg" class="cx-msg" role="status"></span></form></div>
        <div class="hd-card cx-stack cx-administration"><div class="hd-card__title">Room administration</div>
          <p class="cx-sub">Live room data is removed only after its archive is verified.</p>
          <button id="hcn-delete-room" type="button" class="hd-btn hd-btn--danger"${pendingAdministration.has(`archive:${roomId}`) ? ' disabled' : ''}>Archive and delete room</button>
          <span id="hcn-archive-msg" class="cx-msg" role="status">${pendingAdministration.has(`archive:${roomId}`) ? 'Archiving and verifying room data…' : ''}</span></div></section>` : ''}`, strip, turn);

    const log = $('hcn-log');
    let alive = true;
    const roomCurrent = () => alive && isCurrent(turn);
    let inheritedArchivePending = pendingAdministration.has(`archive:${roomId}`);
    let composer = null;
    let since = -1;
    let ctl = null;
    let wake = null;
    let backoff = 1000;
    const stale = new Set();               // panels whose redraw was held back because you were typing in them
    const onVisible = () => { if (!document.hidden) { since = -1; if (wake) { wake(); wake = null; } } };
    document.addEventListener('visibilitychange', onVisible);
    teardown = () => { alive = false; composer?.dispose(); unbindHelpers(); ctl?.abort(); document.removeEventListener('visibilitychange', onVisible); if (wake) wake(); };
    const kick = () => { if (roomCurrent()) { since = -1; ctl?.abort(); } }; // an action just happened: refetch now

    // Replace a container's markup only when it changed and you are not typing in it.
    const put = (id, html) => {
      if (!roomCurrent()) return false;
      const el = $(id);
      if (!el || el.dataset.h === html) return false;
      if (el.contains(document.activeElement) && document.activeElement.matches('input,textarea,select')) { stale.add(id); return false; }
      el.dataset.h = html; el.innerHTML = html; stale.delete(id);
      return true;
    };
    root.addEventListener('focusout', () => setTimeout(() => { if (stale.size && alive) paint(); }, 0));
    // after an inline edit the panel's markup no longer matches what `put` remembers: forget it and refetch
    const redraw = (id) => () => { const el = $(id); if (el) delete el.dataset.h; stale.delete(id); kick(); };

    // ---- chat
    let lastKey = null;
    let lastAt = 0;
    const statusColor = { done: 'var(--status-ok)', failed: 'var(--status-failed)', pending: 'var(--status-attention)', accepted: 'var(--accent)', cancelled: 'var(--text-muted)' };
    const HIDDEN_EVENTS = new Set(['audit_denied', 'identity_linked', 'agent_linked']);
    const tname = (id) => agentName.get(id) || shortId(id);
    function eventRow(e) {
      const row = document.createElement('div');
      const at = new Date(e.created_at);
      const time = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      if (e.event_type === 'message') {
        const agent = e.sender.type === 'agent';
        const mine = !agent && e.sender.id === me.user_id;
        const who = agent ? (agentName.has(e.sender.id) ? `@${agentName.get(e.sender.id)}` : `Agent of ${names.get(e.sender.owner_user_id) || shortId(e.sender.owner_user_id)}`)
          : (mine ? 'You' : (names.get(e.sender.id) || shortId(e.sender.id)));
        const key = `${e.sender.type}:${e.sender.id}`;
        const cont = key === lastKey && at - lastAt < 5 * 60000;
        lastKey = key; lastAt = at.getTime();
        row.className = `cx-m${mine ? ' cx-m--mine' : ''}${agent ? ' cx-m--agent' : ''}${cont ? ' cx-m--cont' : ''}`;
        row.innerHTML = `${mine ? '' : `<span class="cx-av" style="--h:${hueOf(key)}" aria-hidden="true">${agent ? '🤖' : esc(initials(who))}</span>`}<div class="cx-mbody"><div class="cx-meta"><b>${esc(who)}</b><span>${esc(time)}</span></div><div class="cx-bubble conversation-markdown">${renderMarkdown(e.text)}</div></div>`;
        return row;
      }
      lastKey = null;
      if (e.event_type === 'task_created' || e.event_type === 'task_updated') {
        const st = e.task.status || 'pending';
        row.className = 'cx-taskcard';
        row.style.setProperty('--tc', statusColor[st] || 'var(--text-muted)');
        row.innerHTML = `<div class="cx-taskcard__head"><b>@${esc(tname(e.task.target_agent_id))}</b><span>${e.event_type === 'task_created' ? 'new task' : esc(st)}</span><time>${esc(time)}</time></div>${e.event_type === 'task_created' ? `<div>${esc(e.task.text || '')}</div>` : (e.task.result ? `<div>${esc(e.task.result)}</div>` : '')}`;
        return row;
      }
      row.className = 'cx-event';
      row.textContent = e.event_type === 'agent_renamed' ? `@${e.old_name} is now @${e.name}` : e.event_type.replaceAll('_', ' ');
      return row;
    }
    const addEvents = (events) => {
      const shown = events.filter((e) => !HIDDEN_EVENTS.has(e.event_type));
      if (!shown.length) return;
      $('hcn-empty')?.remove();
      const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 60;
      const frag = document.createDocumentFragment();
      for (const e of shown) frag.appendChild(eventRow(e));
      log.appendChild(frag);
      if (stick) log.scrollTop = log.scrollHeight; else $('hcn-jump').hidden = false;
    };
    log.addEventListener('scroll', () => { if (log.scrollTop + log.clientHeight >= log.scrollHeight - 60) $('hcn-jump').hidden = true; });
    $('hcn-jump').onclick = () => { log.scrollTop = log.scrollHeight; };

    // ---- panels, each drawn from the last state only while it is on screen
    const permPills = (actions) => actions.map((a) => pill(PERM_SHORT[a] || a, 'var(--status-ok)')).join(' ');
    const taskChip = (st) => pill(st, ({ done: 'var(--status-ok)', failed: 'var(--status-failed)', pending: 'var(--status-attention)', accepted: 'var(--accent)' })[st] || 'var(--text-muted)');
    const ttlSelect = () => '<label class="cx-msg">Lasts <select name="ttl"><option value="">30 days (default)</option><option value="3600">1 hour</option><option value="86400">1 day</option><option value="604800">7 days</option><option value="7776000">90 days</option></select></label>';
    const expiryOf = (f) => { const v = Number(f.querySelector('select[name="ttl"]')?.value || 0); return v ? Date.now() / 1000 + v : undefined; };
    const warn = (e) => { if (roomCurrent() && !cancelled(e)) alert(e.message); };
    // Locks belong to the mount, not to replaceable forms or a room navigation.
    const agentOperation = (id) => JSON.stringify(['agent', roomId, id]);
    const requestOperation = (id) => JSON.stringify(['request', roomId, id]);
    const refreshAgentControls = () => {
      if (!roomCurrent()) return;
      root.querySelectorAll('[data-rmagent]').forEach(b => { b.disabled = pendingAdministration.has(agentOperation(b.dataset.rmagent)); });
      root.querySelectorAll('form[data-agent], form[data-req]').forEach(f => {
        const key = f.dataset.agent ? agentOperation(f.dataset.agent) : requestOperation(f.dataset.req);
        f.querySelectorAll('button[type="submit"], [data-deny]').forEach(b => { b.disabled = pendingAdministration.has(key); });
      });
      const all = $('hcn-approve-all');
      if (all) all.disabled = (S.requests || []).some(r => pendingAdministration.has(requestOperation(r.request_id)));
    };
    const act = async (fn, keys = []) => {
      if (!roomCurrent() || keys.some(key => pendingAdministration.has(key))) return;
      keys.forEach(key => pendingAdministration.add(key));
      refreshAgentControls();
      try { await fn(); } catch (e) { warn(e); }
      finally {
        keys.forEach(key => pendingAdministration.delete(key));
        if (roomCurrent()) { refreshAgentControls(); kick(); }
      }
    };

    const namedAgent = a => ({...a,display_name:S.flow?.nodes?.find(n=>n.agent_id===a.agent_id)?.fabric?.display_name});
    const drawRoomAgents = () => {
      const mine = new Set(S.mine.map((a) => a.agent_id));
      const html = S.agents.length ? `<ul class="cx-list">${S.agents.map((a) => `<li class="cx-agentrow"><span class="grow">${agentTitle(namedAgent(a), mine.has(a.agent_id) || operator)}<span class="cx-sub1">${esc(a.harness_type)} · ${esc(names.get(a.owner_user_id) || shortId(a.owner_user_id))}</span></span><span class="cx-pills">${permPills(a.actions)}</span>${(canInvite || mine.has(a.agent_id)) ? `<button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-rmagent="${esc(a.agent_id)}">Remove from room</button>` : ''}</li>`).join('')}</ul>` : '<div class="hd-empty">No agents in this room yet.</div>';
      if (put('hcn-roomagents', html)) { $('hcn-roomagents').querySelectorAll('[data-rmagent]').forEach((b) => { b.onclick = () => act(() => api.removeAgent(roomId, b.dataset.rmagent), [agentOperation(b.dataset.rmagent)]); }); wireRename($('hcn-roomagents'), redraw('hcn-roomagents'), turn); }
    };
    const drawTaskTargets = () => {
      const can = S.agents.filter((a) => a.actions.includes('execute'));
      put('hcn-task-target', can.length ? can.map((a) => `<option value="${esc(a.agent_id)}">${esc(namedAgent(a).display_name || `@${a.name}`)}</option>`).join('') : '<option value="">No agent accepts tasks yet</option>');
    };
    const drawTasks = () => {
      const html = S.tasks.length ? `<ul class="cx-list">${S.tasks.slice(0, 30).map((t) => `<li><span class="grow"><b>@${esc(t.target_name || shortId(t.target_agent_id))}</b> ${esc(t.text)}${t.result ? `<br><span class="hd-ident">→ ${esc(t.result)}</span>` : ''}</span>${taskChip(t.status)}${(['pending', 'accepted'].includes(t.status) && canPost) ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-cancel-task="${esc(t.task_id)}">Cancel</button>` : ''}</li>`).join('')}</ul>` : '<div class="hd-empty">No tasks yet.</div>';
      if (put('hcn-tasks', html)) $('hcn-tasks').querySelectorAll('[data-cancel-task]').forEach((b) => { b.onclick = () => act(() => api.cancelTask(roomId, b.dataset.cancelTask)); });
      drawTaskTargets();
    };
    const drawMyAgents = () => {
      if (!$('hcn-grant')) return;
      const live = S.mine.filter((a) => a.status === 'active');
      const html = live.length ? `<ul class="cx-list">${live.map((a) => {
        const g = a.rooms.find((r) => r.room_id === roomId);
        const pend = (a.pending || []).find((r) => r.room_id === roomId);
        const cur = g ? g.actions : (pend ? pend.actions : ['read']);
        return `<li><form class="cx-stack" data-agent="${esc(a.agent_id)}" style="flex:1 1 100%">
          <span class="cx-row">${agentTitle(namedAgent(a), true)} <span class="hd-ident">${esc(shortId(a.agent_id))}</span> ${g ? permPills(g.actions) : ''}${pend ? pill('Waiting for the room owner', 'var(--status-attention)') : ''}</span>
          <span class="cx-checks">${permBoxes(cur, 'p')}</span>${canInvite ? ttlSelect() : ''}
          <button type="submit" class="hd-btn hd-btn--sm">${canInvite ? (g ? 'Update permissions' : 'Add to room') : (g ? 'Request change' : 'Request access')}</button></form></li>`;
      }).join('')}</ul>${canInvite ? '' : '<p class="cx-msg">The room owner approves what each agent may do here.</p>'}` : '<div class="hd-empty">No agents yet. <a href="#/agents" data-go="/agents">Connect one</a>.</div>';
      if (!put('hcn-grant', html)) return;
      const box = $('hcn-grant');
      box.querySelectorAll('[data-go]').forEach((el) => { el.onclick = (e) => { if (!standalone) { e.preventDefault(); go(el.dataset.go, turn); } }; });
      wireRename(box, redraw('hcn-grant'), turn);
      box.querySelectorAll('form[data-agent]').forEach((f) => {
        f.onsubmit = (ev) => {
          ev.preventDefault();
          const actions = picked(f, 'p');
          if (actions.length) act(async () => { await api.grantAgent(roomId, f.dataset.agent, actions, expiryOf(f)); if (roomCurrent()) $('hcn-agent-msg').textContent = canInvite ? 'Agent permissions saved.' : 'Request sent. Waiting for the room owner.'; }, [agentOperation(f.dataset.agent)]);
        };
      });
    };
    const drawRequests = () => {
      const requests = S.requests || [];
      const html = requests.length ? `<ul class="cx-list">${requests.map((r) => `<li><form class="cx-stack" data-req="${esc(r.request_id)}" style="flex:1 1 100%">
          <span><b>@${esc(r.name)}</b> from ${esc(r.owner_email || shortId(r.owner_user_id))}</span>
          <span class="cx-checks">${permBoxes(r.actions, 'p')}</span>${ttlSelect()}
          <span class="cx-row"><button type="submit" class="hd-btn hd-btn--sm" data-decision="approve">Approve</button><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-deny="1">Deny</button></span></form></li>`).join('')}</ul>` : '<div class="hd-empty">No requests.</div>';
      const all = requests.length > 1 ? `<div class="cx-row"><button type="button" class="hd-btn hd-btn--sm" id="hcn-approve-all">Approve all ${requests.length}</button>${ttlSelect().replace('<select', '<select id="hcn-all-ttl"')}</div>` : '';
      if (!put('hcn-requests', all + html)) return;
      const box = $('hcn-requests');
      if (all) {
        $('hcn-approve-all').onclick = () => {
          const v = Number($('hcn-all-ttl').value || 0);
          act(() => api.approveAll(roomId, { request_ids: requests.map((r) => r.request_id), ...(v ? { expires_at: Date.now() / 1000 + v } : {}) }), requests.map(r => requestOperation(r.request_id)));
        };
      }
      box.querySelectorAll('form[data-req]').forEach((f, i) => {
        const asked = requests[i].actions;
        f.querySelectorAll('input[name="p"]').forEach((c) => { if (!asked.includes(c.value)) { c.disabled = true; c.checked = false; } });   // cannot approve more than was asked
        f.onsubmit = (ev) => {
          ev.preventDefault();
          const actions = picked(f, 'p');
          if (actions.length) act(() => api.decideRequest(roomId, f.dataset.req, 'approve', actions, expiryOf(f)), [requestOperation(f.dataset.req)]);
        };
        f.querySelector('[data-deny]').onclick = () => act(() => api.decideRequest(roomId, f.dataset.req, 'deny'), [requestOperation(f.dataset.req)]);
      });
    };

    // ---- agent helpers: panic switch, host-access requests and hosts (operator), profile (owner)
    const loadHelpers = async () => {
      if (!roomCurrent()) return;
      if (operator) {
        try {
          const result = await api.egressRequests();
          if (!roomCurrent()) return;
          S.egress = (result.requests || []).filter((r) => r.room_id === roomId);
        } catch { /* keep the old list */ }
        if (!roomCurrent()) return;
        try {
          const h = await api.helpers();
          if (!roomCurrent()) return;
          S.helpers = h.helpers || []; S.paused = Boolean(h.paused);
        } catch { /* keep */ }
      } else {
        try {
          const result = await api.myHelpers();
          if (!roomCurrent()) return;
          S.helpers = result.helpers || [];
        } catch { /* guests and old servers: no helper list */ }
      }
      if (!roomCurrent()) return;
      if (operator) {
        try {
          const x = await api.egressAllowlist(roomId);
          if (!roomCurrent()) return;
          S.allow = x.allowlist || []; S.entries = Array.isArray(x.entries) ? x.entries : null;
        } catch { /* keep */ }
      }
      if (!roomCurrent()) return;
      if (canInvite) {
        try {
          const profile = await api.roomProfile(roomId);
          if (!roomCurrent()) return;
          S.profile = profile;
        } catch { /* not readable: keep */ }
      }
    };
    const drawHelpers = () => {
      if (operator) {
        put('hcn-panic', renderPanic(S.paused, { operator }));
        put('hcn-egress', renderEgressRequests(S.egress, { operator }));
        put('hcn-hosts-table', renderHostsTable(S.allow || [], S.egress, { operator, room: roomId, permanent: S.profile && Array.isArray(S.profile.permanent_hosts) ? S.profile.permanent_hosts : null, entries: S.entries }));
      }
      if (canInvite && S.profile) put('hcn-profile-box', renderProfileEditor(S.profile, { owner: canInvite }));
    };
    const unbindHelpers = wireHelperHandlers(root, {
      api, roomId, operator, owner: canInvite, act, getProfile: () => S.profile, setProfile: (p) => { if (!roomCurrent()) return; S.profile = p; $('hcn-profile-msg').textContent = 'Saved.'; },
      onError: (m) => { if (roomCurrent()) $('hcn-profile-msg').textContent = m; },
      refresh: async () => { if (!roomCurrent()) return; await loadHelpers(); if (roomCurrent()) drawHelpers(); },
      readForm: (f) => { const fd = new FormData(f); return { services: fd.get('services'), ssh_secret_names: fd.get('ssh_secret_names'), allowed_models: fd.get('allowed_models'), allowed_families: fd.getAll('allowed_families'), tools_allowed: fd.get('tools_allowed') !== null, permanent_hosts: fd.get('permanent_hosts') }; },
    });

    // ---- live flow: nodes are people/agents, edges are tasks; the dashboard adds fabric session state
    let flowSig = new Map();
    const showDetail = (html) => { const d = $('hcn-flow-detail'); if (d) d.innerHTML = html; };
    let flowKey = '';
    const drawFlow = () => {
      const box = $('hcn-flow');
      const flow = S.flow && {...S.flow,nodes:S.flow.nodes.map(n=>({...n,name:n.fabric?.display_name || n.name}))};
      if (!box || !flow) return;
      const key = JSON.stringify(flow);
      if (key === flowKey) return;                  // nothing moved: leave the animation alone
      flowKey = key;
      if (!flow.nodes.length) { box.innerHTML = '<div class="hd-empty">No agents in this room yet.</div>'; return; }
      flowSig = renderFlow(box, flow, {
        prev: flowSig,
        onNode: (n) => {
          const mine = flow.edges.filter((e) => e.from === n.id || e.to === n.id).map((e) => `${esc(flow.nodes.find((x) => x.id === e.from)?.name)} → ${esc(flow.nodes.find((x) => x.id === e.to)?.name)}: ${esc(Object.entries(e.counts).map(([k, v]) => `${v} ${k}`).join(', '))}`);
          const f = n.fabric ? `<br>Fabric agent ${esc(n.fabric.status || '')}${n.fabric.host ? ` on ${esc(n.fabric.host)}` : ''}${n.fabric.mission_title ? ` · mission ${esc(n.fabric.mission_title)}` : ''}${n.fabric.branch ? ` · ${esc(n.fabric.branch)}` : ''}${n.fabric.parent_session_id ? ` · started by agent ${esc(n.fabric.parent_session_id)}` : ''}${nativeSessionLink(n.fabric.session_id) ? `<br>${nativeSessionLink(n.fabric.session_id)}` : ''}` : '';
          showDetail(`<b>${esc(n.fabric?.display_name || (n.kind === 'agent' ? `@${n.name}` : n.name))}</b> ${n.owner_email ? `(${esc(n.owner_email)}) ` : ''}${n.actions && n.actions.length ? permPills(n.actions) : ''}${n.group ? `<br>herdr workspace ${esc(n.group)}` : ''}${n.parent_agent_id ? `<br>started under @${esc((flow.nodes.find((x) => x.agent_id === n.parent_agent_id) || {}).name || 'another agent')}` : ''}${f}${mine.length ? `<br>${mine.join('<br>')}` : ''}${n.kind === 'agent' ? renderHelperTree(n.agent_id, flow, S.helpers, { operator, viewerId: me && me.user_id }) : ''}`);
        },
        onEdge: (e) => {
          const ts = flow.tasks.filter((t) => t.from === e.from && t.to === e.to).slice(0, 5);
          showDetail(ts.map((t) => `${taskChip(t.status)} ${esc(t.text)}`).join('<br>') || 'No recent tasks.');
        },
      });
    };

    const memberAction = async (button, action, success, rollback = () => {}) => {
      const key = `members:${roomId}`;
      if (!roomCurrent()) return;
      if (pendingAdministration.has(key)) { rollback(); return; }
      pendingAdministration.add(key);
      const msg = $('hcn-members-msg');
      msg.setAttribute('role', 'status'); msg.textContent = 'Saving…';
      button.disabled = true;
      try {
        await action();
        if (!roomCurrent()) return;
        msg.textContent = success;
        $('hcn-people').dataset.h = '';
      } catch (e) {
        if (!roomCurrent()) return;
        rollback();
        if (!cancelled(e)) { msg.setAttribute('role', 'alert'); msg.textContent = e.message; }
        else msg.textContent = '';
      } finally {
        pendingAdministration.delete(key);
        if (roomCurrent()) { button.disabled = false; await drawPeople(); }
      }
    };
    if ($('hcn-delete-room')) $('hcn-delete-room').onclick = async () => {
      const key = `archive:${roomId}`, button = $('hcn-delete-room'), msg = $('hcn-archive-msg');
      if (!roomCurrent() || pendingAdministration.has(key) || button.disabled || !confirmDeleteRoom(info)) return;
      pendingAdministration.add(key); button.disabled = true;
      msg.setAttribute('role', 'status'); msg.textContent = 'Archiving and verifying room data… Live deletion is not yet confirmed.';
      try {
        const result = await api.archiveRoom(roomId);
        if (!roomCurrent()) return;
        if (result.status !== 'verified' || result.live_room_id !== null) throw new Error('Room deletion is not complete. The archive must be verified and live deletion confirmed before completion.');
        dispose();
        // Stop other room mutations once the server confirms live deletion.
        root.querySelectorAll('.cx-panel button, .cx-panel input, .cx-panel select, .cx-panel textarea').forEach(el => { el.disabled = true; });
        msg.textContent = 'Archive verified. Room deleted.';
      } catch (e) {
        if (!roomCurrent()) return;
        button.disabled = false;
        if (!cancelled(e)) { msg.setAttribute('role', 'alert'); msg.textContent = `${e.message} Deletion is not confirmed; retry or refresh to check the room.`; }
        else msg.textContent = 'Deletion check cancelled. Refresh to check the room before retrying.';
      } finally { pendingAdministration.delete(key); }
    };
    let peopleBusy = false;
    const drawPeople = async () => {
      if (!roomCurrent() || !canInvite || peopleBusy || pendingAdministration.has(`members:${roomId}`)) return;
      peopleBusy = true;
      try {
        const [room, inv] = await Promise.all([api.room(roomId), api.invites(roomId)]);
        if (!roomCurrent() || pendingAdministration.has(`members:${roomId}`)) return;
        names = new Map((room.members || []).map((m) => [m.user_id, m.email || shortId(m.user_id)]));
        const html = renderRoomAdministration({ room, invites: inv.invites, canManage: canInvite });
        if (put('hcn-people', html)) {
          const box = $('hcn-people');
          box.querySelectorAll('[data-rm]').forEach(b => { b.onclick = () => {
            const member = room.members.find(m => m.user_id === b.dataset.rm);
            if (!roomCurrent() || pendingAdministration.has(`members:${roomId}`)) return;
            if (confirmAction(`Remove ${member.email || member.user_id} from this room? Their room access and delegated permissions stop. Messages and files remain; their account and other rooms are unchanged.`))
              memberAction(b, () => api.removeMember(roomId, b.dataset.rm), 'Removed from room. Messages and files remain.');
          }; });
          box.querySelectorAll('[data-approve-member]').forEach(b => { b.onclick = () => memberAction(b, () => api.approveMember(roomId, b.dataset.approveMember), 'Member approved.'); });
          box.querySelectorAll('[data-upload]').forEach(b => { b.onchange = () => {
            const allowed = b.checked;
            memberAction(b, () => api.setUploadPermission(roomId, b.dataset.upload, allowed), 'Upload permission saved.', () => { b.checked = !allowed; });
          }; });
          box.querySelectorAll('[data-cancel]').forEach(b => { b.onclick = () => memberAction(b, () => api.cancelInvite(roomId, b.dataset.cancel), 'Invitation cancelled.'); });
        }
      } catch { /* keep what is shown */ } finally { peopleBusy = false; }
    };

    const setBadge = (k, n) => { const b = $(`hcn-b-${k}`); if (!b) return; b.hidden = !n; b.textContent = n > 99 ? '99+' : String(n); };
    const loadMine = async () => {
      try {
        const { agents } = await api.agents();
        if (!roomCurrent()) return;
        S.mine = agents;
      } catch { /* keep the old list */ }
    };
    function paint() {
      setBadge('agents', (S.requests || []).length);
      setBadge('tasks', S.tasks.filter((t) => ['pending', 'accepted'].includes(t.status)).length);
      if (sub === 'tasks') drawTasks();
      else if (sub === 'flow') drawFlow();
      else if (sub === 'agents') { drawRoomAgents(); drawMyAgents(); drawRequests(); drawHelpers(); refreshAgentControls(); }
    }
    const paintPanel = async () => {
      if (!roomCurrent()) return;
      if (sub === 'agents' && canPost) await loadMine();
      if (!roomCurrent()) return;
      if (sub === 'agents') await loadHelpers();
      if (!roomCurrent()) return;
      if (sub === 'people') await drawPeople();
      if (!roomCurrent()) return;
      paint();
    };
    const showSub = (k) => {
      if (!roomCurrent()) return;
      sub = k; subTab.set(roomId, k);
      root.querySelectorAll('[data-sub]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.sub === k)));
      root.querySelectorAll('.cx-panel').forEach((p) => { p.hidden = p.id !== `hcn-p-${k}`; });
      if (k === 'chat') { log.scrollTop = log.scrollHeight; composer?.focus(); }
      paintPanel();
    };
    root.querySelectorAll('[data-sub]').forEach((b) => { b.onclick = () => showSub(b.dataset.sub); });
    root.querySelectorAll('.cx-panel').forEach((p) => { p.hidden = p.id !== `hcn-p-${sub}`; });

    const refreshArchiveControl = () => {
      if (!inheritedArchivePending || pendingAdministration.has(`archive:${roomId}`)) return;
      inheritedArchivePending = false;
      const button = $('hcn-delete-room');
      if (!button) return;
      button.disabled = false;
      $('hcn-archive-msg').textContent = 'Previous deletion request finished. The room is still available; refresh or retry to check deletion status.';
    };

    // ---- one long-poll feeds every panel (replaces five timers); it sleeps while the tab is hidden
    const apply = (s) => {
      if (s.agents) { S.agents = s.agents; S.agents.forEach((a) => agentName.set(a.agent_id, a.name)); }
      S.tasks = s.tasks || []; S.flow = s.flow || null; S.requests = s.requests || null;
      S.agents.forEach(a=>agentName.set(a.agent_id,namedAgent(a).display_name || a.name));
      addEvents(s.events || []);
      cursor = s.next_cursor ?? cursor;
      paintPanel();
    };
    const sleep = (ms) => new Promise((r) => { setTimeout(r, ms); });
    (async () => {
      while (roomCurrent()) {
        if (document.hidden) { await new Promise((r) => { wake = r; }); continue; }
        ctl = new AbortController();
        try {
          const s = await api.state(roomId, since, cursor, 8, ctl.signal);
          if (!roomCurrent()) return;
          backoff = 1000;
          refreshArchiveControl();
          refreshAgentControls();
          if (s.changed) { apply(s); since = s.has_more ? -1 : s.seq; } else since = s.seq;
        } catch (e) {
          if (!roomCurrent()) return;
          if (e.name === 'AbortError') continue;
          if (e instanceof AuthError) { dispose(); store.clear(); start(); return; }
          if (e.code === 'not_found') { dispose(); go('/rooms', turn); return; }
          await sleep(backoff);
          if (!roomCurrent()) return;
          backoff = Math.min(backoff * 2, 15000);
        }
      }
    })();

    // ---- forms
    const taskForm = $('hcn-task');
    if (taskForm) {
      taskForm.onsubmit = async (ev) => {
        ev.preventDefault();
        if (!roomCurrent()) return;
        const f = new FormData(taskForm);
        try {
          await api.createTask(roomId, f.get('target'), f.get('text'));
          if (!roomCurrent()) return;
          taskForm.querySelector('textarea').value = ''; $('hcn-task-msg').textContent = ''; kick();
        } catch (e) { if (roomCurrent() && !cancelled(e)) $('hcn-task-msg').textContent = e.message; }
      };
    }
    if ($('hcn-composer')) {
      composer = mountComposer($('hcn-composer'), {
        draftKey: `collab:${me.user_id}:${roomId}`,
        placeholder: 'Message the room… type @ to hand an agent a task',
        hint: 'Enter sends · Shift+Enter adds a line · @agent … gives that agent a task',
        suggestions: () => S.agents.filter((a) => a.actions.includes('execute')).map((a) => ({
          id: a.agent_id, label: namedAgent(a).display_name || `@${a.name}`, insertText: `@${a.name}`,
          detail: `${a.harness_type} · ${names.get(a.owner_user_id) || shortId(a.owner_user_id)}`,
        })),
        onSend: async (text) => {
          if (!roomCurrent()) return;
          const mention = /^@([a-z0-9-]{1,32})\s+([\s\S]+)$/.exec(text.trim());
          if (mention) { // Preserve the room's authenticated leading-agent task contract.
            const hits = S.agents.filter((a) => a.name === mention[1] && a.actions.includes('execute'));
            if (hits.length !== 1) throw new Error(hits.length ? `More than one agent is called @${mention[1]}; use the Tasks tab` : `No agent named @${mention[1]} in this room accepts tasks`);
            await api.createTask(roomId, hits[0].agent_id, mention[2]);
          } else await api.say(roomId, text);
          if (roomCurrent()) { kick(); log.scrollTop = log.scrollHeight; }
          return { status: 'delivered', message: mention ? 'Task created' : 'Message posted' };
        },
      });
    }
    const inviteForm = $('hcn-invite');
    if (inviteForm) {
      inviteForm.onsubmit = async (ev) => {
        ev.preventDefault();
        if (!roomCurrent()) return;
        const f = new FormData(ev.target);
        const msg = $('hcn-invite-msg');
        try {
          await api.invite(roomId, f.get('email'), f.get('role'));
          if (!roomCurrent()) return;
          msg.textContent = 'Invited. They get access when they sign in here.';
          ev.target.reset(); $('hcn-people').dataset.h = ''; paintPanel();
        } catch (e) { if (roomCurrent() && !cancelled(e)) msg.textContent = e.message; }
      };
    }
    log.scrollTop = log.scrollHeight;
  }

  if (standalone) window.addEventListener('hashchange', onHash);
  const ready = start();
  return { ready, get state() { return { ...routeState }; },
    dispose: () => { disposed = true; generation += 1; clearTimeout(waitTimer); dispose(); window.removeEventListener('hashchange', onHash); } };
}

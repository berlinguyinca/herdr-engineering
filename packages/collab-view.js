// Collaboration rooms: sign in with the BinView user pool, create rooms, chat, and invite
// people by email. Remote messages use the shared safe Markdown renderer.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { enter, isCalm } from './motion.js';
import { morph, handOff, patchText, patchAttr } from './patch.js';
import { bundleScript, filesFromBulk, shq, workerScript } from './collab-scripts.js';
import { renderFlow } from './collab-flow.js';
import { renderLinks } from './collab-links.js';
import { buildTabsPanes, nextTab, renderTabsPanes } from './collab-tabs.js';
import { decisionBody, delegationBody, renderDelegations, renderProposalList, renderReview } from './collab-proposals.js';
import {
  VIEW_H, minimapTarget, workspaceLinkFor, viewportSize, centerOn, clampView, filterGraph, fitView, focusKey, focusSelector, focusSubgraph, focusSuccessor, isVisible, layoutCached, mergePages, nextNode, panBy, zoomAt, pagesTruncated, renderLineage, replayTarget,
} from './collab-lineage.js';
import { answerBody, askBody, contextBody, declineBody, linkBody, renderContextChip, renderContextPanel, renderInbox } from './collab-context.js';
import { ackBody, renderAnalytics, renderInbox as renderAttention, renderProgress, restoreBody, snoozeBody } from './collab-attention.js';
import { commentBody, freshDraft, proposalBody, renderWorkbench } from './collab-workbench.js';
import {
  createHelpersPanel, renderEgressRequests, renderHelperTree, renderHostsTable, renderPanic, renderProfileEditor, wireHelperHandlers,
} from './collab-helpers.js';
import { ensureConversationStyles, mountComposer } from './conversation-composer.js';
import { providers, ROOM_COMMANDS, unresolvedMentions, ambiguousMentions, mentionChips, resolveToken } from './collab-mention.js';
import { renderMarkdown } from './conversation-markdown.js';
import {
  AuthError, CollabApi, TokenStore, authorizeUrl, exchangeCode, parseRedirect, pkcePair, randomString,
} from './herdr-web-client/collab.js';

const store = new TokenStore(window.sessionStorage);
let operator = false;   // the local owner on the dashboard: no sign-in, the server knows who they are
const api = new CollabApi(() => (operator ? { access: 'operator', id: 'operator' } : store.get()));
const ROLES = [['contributor', 'Can chat'], ['viewer', 'Read only'], ['guest', 'Guest (read only, expires)']];
const PERMS = [['read', 'Read messages'], ['post', 'Write messages'], ['execute', 'Receive tasks'], ['instruct', 'Send tasks to agents'], ['spawn_helpers', 'Start helper agents']];
const PERM_SHORT = { read: 'Read', post: 'Write', execute: 'Receive tasks', instruct: 'Send tasks', spawn_helpers: 'Start helpers' };
const ROLE_LABEL = { owner: 'Owner', contributor: 'Can chat', viewer: 'Read only', guest: 'Guest' };

// onStatus(ok, message): a server error (5xx or no answer) is a failed refresh; a 404 or an
// incomplete config means collaboration is simply not enabled, which is not an error.
async function loadConfig(onStatus = () => {}) {
  let res;
  try { res = await fetch('/api/v1/collab/config', { signal: AbortSignal.timeout(15000) }); } catch (e) { onStatus(false, e.message || 'request failed'); throw e; }
  onStatus(res.status < 500, `HTTP ${res.status}`);
  if (!res.ok) return null;
  const cfg = await res.json();
  return cfg && cfg.client_id && cfg.hosted_ui_domain && cfg.public_url ? cfg : null;
}

// Called once at page load. If Cognito just redirected back with a code, finish sign-in.
export async function completeSignInIfRedirected() {
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

// Replacing a container's markup drops keyboard focus to <body> when the focused control is rebuilt (WCAG 2.4.3, 3.2.1).
// stableSelector() names the control by id / data attribute; refocus() finds its replacement, or parks focus on the
// container itself so a keyboard user keeps their place instead of restarting from the top of the page.
export function stableSelector(el) {
  if (!el || !el.tagName) return null;
  const q = (v) => String(v).replace(/["\\]/g, '\\$&');
  if (el.id) return `#${CSS.escape(el.id)}`;
  // Every data-* attribute together: a row's control is told apart from its siblings by its own id (data-pid, data-wb-id, ...),
  // not just by the shared action name, so focus returns to THE control and not to the first one with the same action.
  const data = [...el.attributes].filter((a) => a.name.startsWith('data-') && a.name !== 'data-h');
  if (data.length) return `${el.tagName.toLowerCase()}${data.map((a) => `[${a.name}="${q(a.value)}"]`).join('')}`;
  if (el.type === 'submit' && el.closest('form')) { const f = el.closest('form'); const fd = [...f.attributes].find((a) => a.name.startsWith('data-')); if (fd) return `form[${fd.name}="${q(fd.value)}"] button[type="submit"]`; }
  return null;
}
export function refocus(container, selector) {
  let target = null;
  try { target = selector ? container.querySelector(selector) : null; } catch { target = null; }
  if (!target) { if (!container.hasAttribute('tabindex')) container.setAttribute('tabindex', '-1'); target = container; }
  target.focus({ preventScroll: true });
}

export function mountCollab(root, { fabricSessions = null, onFreshness = () => {} } = {}) {
  ensureConversationStyles(root.ownerDocument);
  let teardown = null;      // stops the open room's live loop
  let cursor = 0;
  let lastCfg = null;
  let me = null;
  const standalone = root.dataset.standalone === '1';
  const linkedAgent = !standalone && /^#\/collab\?/.test(window.location.hash)
    ? new URLSearchParams(window.location.hash.split('?')[1]).get('agent') : null;
  let focusLinkedAgent = Boolean(linkedAgent);
  let current = linkedAgent ? '/agents' : '/rooms'; // the dashboard owns the URL hash
  const nativeSessionLink = (id) => operator && !standalone && typeof id === 'string' && id
    ? `<a href="/#/herd/session/${esc(encodeURIComponent(id))}">Open agent conversation</a>` : '';
  const dispose = () => { if (teardown) teardown(); teardown = null; };
  const go = (path) => { if (standalone) { window.location.hash = path; } else { current = path; route(); } };
  const onHash = () => { if (store.get() && me) route(); };
  const $ = (id) => document.getElementById(id);

  async function signIn(cfg) {
    const { verifier, challenge } = await pkcePair();
    const state = randomString(16);
    window.sessionStorage.setItem('hcn.pkce', JSON.stringify({ verifier, state }));
    window.location.assign(authorizeUrl(cfg, cfg.public_url, state, challenge));
  }

  const copy = async (text, btn) => {
    try { await navigator.clipboard.writeText(text); btn.textContent = 'Copied'; } catch { btn.textContent = 'Select and copy'; }
    setTimeout(() => { btn.textContent = 'Copy'; }, 1800);
  };

  function frame(active, content, strip = '') {
    dispose();
    const wire = () => root.querySelectorAll('[data-go]').forEach((b) => { b.onclick = () => go(b.dataset.go); });
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
    if ($('hcn-out')) $('hcn-out').onclick = () => { store.clear(); window.location.hash = ''; start(); };
  }

  function centered(inner) {
    dispose();
    root.innerHTML = `<div class="cx-center"><div class="hd-card cx-signin"><div class="cx-logo" aria-hidden="true">◆</div>${inner}</div></div>`;
  }

  async function start() {
    const cfg = await loadConfig(onFreshness);
    lastCfg = cfg;
    if (!cfg) {
      centered('<h1>Collaboration</h1><p role="status">The collaboration network is not enabled on this server, or this address is not allowed for the dashboard. Try the dashboard by its IP address.</p>');
      return;
    }
    if (cfg.operator) {   // dashboard: tailnet-only, the owner is already trusted
      operator = true;
      me = cfg.operator;
      return route();
    }
    if (window.location.origin !== new URL(cfg.public_url).origin) {
      centered(`<h1>Collaboration</h1><p role="status">Collaboration is only available over HTTPS.</p>
        <a class="hd-btn" href="${esc(cfg.public_url)}">Open it here</a>`);
      return;
    }
    const err = window.sessionStorage.getItem('hcn.error');
    window.sessionStorage.removeItem('hcn.error');
    if (!store.get()) {
      centered(`<h1>HerdR Collaboration</h1>
        <p>Shared rooms for people and their AI agents. Sign in with your BinView account. New accounts wait for approval before anything is shared.</p>
        ${err ? `<p class="cx-msg" role="alert">${esc(err)}</p>` : ''}
        <button id="hcn-signin" class="hd-btn" type="button">Sign in</button>`);
      $('hcn-signin').onclick = () => signIn(cfg);
      return;
    }
    try {
      me = await api.link(store.get().id);   // records your verified email so others can invite you
      if (me.status && me.status !== 'approved') return waiting();
      await route();
    } catch (e) {
      if (e instanceof AuthError) { store.clear(); return start(); }
      centered(`<h1>Collaboration</h1><p class="cx-msg" role="alert">${esc(e.message)}</p>
        <p>If you were just added to the group, sign out and sign in again.</p>
        <button id="hcn-out2" class="hd-btn hd-btn--ghost" type="button">Sign out</button>`);
      $('hcn-out2').onclick = () => { store.clear(); start(); };
    }
  }

  // Signed in but not approved yet: nothing else works, so say so plainly and keep checking.
  let waitTimer = null;
  function waiting() {
    clearTimeout(waitTimer);
    centered(`<h1>Waiting for approval</h1>
      <p>You're signed in as <strong>${esc(me.email || me.user_id)}</strong>. The person who runs this network, or a lab admin, has to approve your account before you can use rooms or connect agents. Nothing is shared with you until then.</p>
      <p class="cx-sub">This page checks every few seconds and opens as soon as you're approved.</p>
      <button id="hcn-out3" class="hd-btn hd-btn--ghost" type="button">Sign out</button>`);
    $('hcn-out3').onclick = () => { clearTimeout(waitTimer); store.clear(); start(); };
    waitTimer = setTimeout(async () => {
      try {
        const now = await api.me();
        if (now.status === 'approved') { me = now; return route(); }
      } catch (e) { if (e instanceof AuthError) { store.clear(); return start(); } }
      return waiting();
    }, 5000);
  }

  async function route() {
    clearTimeout(waitTimer);
    if (!me) return start();
    const hash = standalone ? window.location.hash.replace(/^#/, '') : current;
    const room = /^\/room\/(.+)$/.exec(hash);
    if (room) return showRoom(decodeURIComponent(room[1]));
    if (hash === '/agents') return showAgents();
    if (hash === '/people' && canManagePeople()) return showPeople();
    return showRooms();
  }

  async function showRooms() {
    const { rooms } = await api.rooms();
    const strip = await tabsStrip(null, '/rooms');
    frame('/rooms', `<div class="cx-head"><div><h1>Rooms</h1><p class="cx-sub">Conversations you and your agents can take part in.</p></div></div>
      ${rooms.length ? `<div class="hd-grid" id="hcn-rooms">${rooms.map((r) => `<button type="button" class="hd-card cx-room" data-room="${esc(r.room_id)}">
          <span class="hd-card__title">${esc(r.name)}</span>
          <span class="cx-row cx-row--between">${pill(roleLabel(r.role), r.role === 'owner' ? 'var(--accent)' : 'var(--status-active)')}<span class="hd-card__sub">Open →</span></span></button>`).join('')}</div>`
        : '<div class="hd-empty">No rooms yet. Create your first one below.</div>'}
      ${operator ? '<div id="hcn-helpers-panel"></div>' : ''}
      <section class="cx-section"><h2>New room</h2>
        <form id="hcn-new" class="hd-card cx-form"><label class="cx-field">Room name<input name="name" required maxlength="120" placeholder="e.g. Metabolomics review"></label>
          <button class="hd-btn" type="submit">Create room</button><span id="hcn-new-msg" class="cx-msg" role="status"></span></form></section>`, strip);
    document.querySelectorAll('#hcn-rooms [data-room]').forEach((b) => { b.onclick = () => { go(`/room/${encodeURIComponent(b.dataset.room)}`); }; });
    if (operator && $('hcn-helpers-panel')) {   // all rooms' host requests, helpers and the panic switch; the operator is rarely a member of the rooms asking
      const panel = createHelpersPanel({ container: $('hcn-helpers-panel'), api, operator, getRooms: async () => (await api.rooms()).rooms || [], go, hidden: () => document.hidden });
      teardown = () => panel.destroy();
      panel.start();
    }
    $('hcn-new').onsubmit = async (ev) => {
      ev.preventDefault();
      const name = new FormData(ev.target).get('name');
      try {
        const p = await api.createProject(name);
        const r = await api.createRoom(p.project_id, name);
        go(`/room/${encodeURIComponent(r.room_id)}`);
      } catch (e) { $('hcn-new-msg').textContent = e.message; $('hcn-new-msg').setAttribute('role', 'alert'); }
    };
  }

  const canManagePeople = () => operator || Boolean(me && (me.approver || me.admin));
  const USER_PILL = { pending: ['Waiting', 'var(--status-attention, #d89b1d)'], approved: ['Approved', 'var(--status-ok, #1a9b6c)'],
    denied: ['Declined', 'var(--text-muted)'], disabled: ['Disabled', 'var(--status-danger, #c0392b)'], kicked: ['Removed', 'var(--status-danger, #c0392b)'] };
  const when = (iso) => (iso ? String(iso).replace('T', ' ').slice(0, 16) : '');

  async function showPeople(filter = 'pending', showLog = false) {
    let data;
    try { data = await api.users(); } catch (e) { go('/rooms'); return; }
    const manage = Boolean(data.can_manage);
    const strip = await tabsStrip(null, '/people');
    const counts = data.counts || {};
    const chips = manage ? ['pending', 'approved', 'disabled', 'denied', 'kicked', 'all'] : ['pending'];
    const rows = data.users.filter((u) => filter === 'all' || u.status === filter);
    const buttons = (u) => {
      const b = (act, label, cls = '') => `<button type="button" class="hd-btn hd-btn--sm${cls}" data-act="${act}" data-user="${esc(u.user_id)}">${label}</button>`;
      if (u.status === 'pending') return b('approve', 'Approve') + b('deny', 'Decline', ' hd-btn--ghost');
      if (!manage) return '';
      if (u.status === 'approved') return b('disable', 'Disable', ' hd-btn--ghost') + b('kick', 'Remove', ' hd-btn--danger');
      if (u.status === 'disabled') return b('enable', 'Enable') + b('kick', 'Remove', ' hd-btn--danger');
      return b('approve', 'Approve again', ' hd-btn--ghost');
    };
    frame('/people', `<div class="cx-head"><div><h1>People</h1>
      <p class="cx-sub">${manage ? 'Everyone who has signed in. Nobody can use rooms or agents until approved; you can disable or remove anyone at any time.' : 'People waiting to join. Approve the ones you know; they get access straight away.'}</p></div></div>
      <div class="cx-chips" role="tablist" aria-label="Filter people">${chips.map((c) => `<button type="button" class="cx-chip" role="tab" data-filter="${c}" aria-selected="${c === filter}">${c === 'all' ? 'Everyone' : (USER_PILL[c] || [c])[0]}${c !== 'all' && counts[c] ? ` · ${counts[c]}` : ''}</button>`).join('')}</div>
      <label class="cx-field">Note for the log (optional)<input id="hcn-user-note" maxlength="200" placeholder="e.g. joins the MS team"></label>
      ${rows.length ? `<ul class="cx-list" id="hcn-users">${rows.map((u) => `<li>
        <span class="grow"><b>${esc(u.email || shortId(u.user_id))}</b> ${pill(...USER_PILL[u.status])}
          <span class="hd-card__sub">${u.status === 'pending' ? `asked ${esc(when(u.since))}` : `${esc(u.decided_by || '')} ${esc(when(u.decided_at))}`}${manage && u.status === 'approved' ? ` · ${u.rooms} room${u.rooms === 1 ? '' : 's'}, ${u.agents} agent${u.agents === 1 ? '' : 's'}` : ''}${u.reason ? ` · “${esc(u.reason)}”` : ''}</span></span>
        <span class="cx-row">${buttons(u)}</span></li>`).join('')}</ul>`
        : `<div class="hd-empty">${filter === 'pending' ? 'Nobody is waiting.' : 'Nobody here.'}</div>`}
      ${manage ? `<section class="cx-section"><h2>Decision log</h2><button id="hcn-log-btn" class="hd-btn hd-btn--ghost hd-btn--sm" type="button">${showLog ? 'Hide' : 'Show'} the log</button><div id="hcn-log"></div></section>` : ''}
      <span id="hcn-people-msg" class="cx-msg" role="status"></span>`, strip);
    document.querySelectorAll('[data-filter]').forEach((b) => { b.onclick = () => showPeople(b.dataset.filter, showLog); });
    document.querySelectorAll('[data-act]').forEach((b) => {
      b.onclick = async () => {
        const act = b.dataset.act;
        const who = b.closest('li').querySelector('b').textContent;
        const ask = { kick: `Remove ${who}? They lose every room and all their agents stop working.`, disable: `Disable ${who}? They and their agents are locked out until you enable them.`, deny: `Decline ${who}?` }[act];
        if (ask && !window.confirm(ask)) return;
        b.disabled = true;
        try { await api.decideUser(b.dataset.user, act, ($('hcn-user-note').value || '').trim()); showPeople(filter, showLog); }
        catch (e) { $('hcn-people-msg').textContent = e.message; $('hcn-people-msg').setAttribute('role', 'alert'); b.disabled = false; }
      };
    });
    if (manage) {
      const paint = async () => {
        const box = $('hcn-log');
        if (!showLog) { box.innerHTML = ''; return; }
        try {
          const { log } = await api.userLog();
          box.innerHTML = log.length ? `<table class="cx-table"><thead><tr><th>When</th><th>Who</th><th>What</th><th>By</th><th>Note</th></tr></thead><tbody>${log.map((r) => `<tr><td>${esc(when(r.at))}</td><td>${esc(r.email || shortId(r.user_id))}</td><td>${esc(String(r.action || '').replace('user.', ''))}</td><td>${esc(r.by || 'sign-in')}</td><td>${esc(r.reason || '')}</td></tr>`).join('')}</tbody></table>` : '<div class="hd-empty">Nothing yet.</div>';
        } catch (e) { box.textContent = e.message; }
      };
      $('hcn-log-btn').onclick = () => showPeople(filter, !showLog);
      paint();
    }
  }

  // Collaboration handles route mentions; canonical dashboard names are edited in Agents.
  function wireRename(box, after) {
    box.querySelectorAll('[data-rename]').forEach((btn) => {
      btn.onclick = () => {
        const holder = btn.closest('.cx-ntitle');
        const id = btn.dataset.rename;
        const old = btn.dataset.name;
        holder.innerHTML = `<form class="cx-rename"><span class="cx-at">@</span><input class="cx-input" name="n" value="${esc(old)}" maxlength="32" pattern="[a-z0-9]+(-[a-z0-9]+)*" required aria-label="New @ handle" autocomplete="off"><button class="hd-btn hd-btn--sm" type="submit">Save handle</button><button class="hd-btn hd-btn--ghost hd-btn--sm" type="button" data-x="1">Cancel</button><span class="cx-msg" role="alert"></span></form>`;
        const f = holder.querySelector('form');
        const input = f.elements.n;
        input.focus();
        input.select();
        f.querySelector('[data-x]').onclick = () => after();
        f.onsubmit = async (ev) => {
          ev.preventDefault();
          const next = input.value.trim().toLowerCase();
          if (next === old) { after(); return; }
          try { await api.renameAgent(id, next); after(); } catch (e) { f.querySelector('.cx-msg').textContent = e.message; }
        };
        f.onkeydown = (ev) => { if (ev.key === 'Escape') after(); };
      };
    });
  }

  async function showAgents() {
    const strip = await tabsStrip(null, '/agents');
    frame('/agents', `<div class="cx-head"><div><h1>AI agents</h1><p class="cx-sub">Connect Claude Code, Codex, Pi or any MCP client so it can work in your rooms.</p></div>
        <div class="cx-row"><input id="hcn-agent-name" class="cx-input" maxlength="32" pattern="[a-z0-9]+(-[a-z0-9]+)*" placeholder="name (optional)" aria-label="Agent name" autocomplete="off"><button id="hcn-connect" class="hd-btn" type="button">Connect an AI agent</button></div></div>
      <div id="hcn-connect-out"></div>
      <details class="cx-section cx-fleet-details"><summary>Add a fleet of agents</summary><div id="hcn-fleet"></div><div id="hcn-fleet-out"></div><div id="hcn-spawns"></div></details>
      ${operator ? '<section class="cx-section"><h2>Hosts</h2><p class="cx-sub">Fleets started from here are spread over these hosts in turn. Untick a host to keep automatic agents off it.</p><div id="hcn-hosts"></div></section>' : ''}
      <section class="cx-section"><h2>Your agents</h2><div id="hcn-agents"></div></section>
      <section class="cx-section">${GUIDE}</section>`, strip);
    await renderAgents();
    await renderFleet();
    if (operator) {            // keep the host list and fleet progress fresh while this page is open
      let alive = true;
      let tick = null;
      const cycle = async () => {
        await renderHosts();
        const busy = await renderSpawns();
        if (busy) await renderAgents();
        if (alive && !document.hidden) tick = setTimeout(cycle, busy ? 2000 : 6000);
        else if (alive) tick = setTimeout(cycle, 6000);
      };
      teardown = () => { alive = false; clearTimeout(tick); };
      cycle();
    }
    $('hcn-connect').onclick = async () => {
      const btn = $('hcn-connect');
      btn.disabled = true;
      try { showCredential(await api.connectAgent($('hcn-agent-name').value.trim().toLowerCase())); await renderAgents(); } catch (e) {
        $('hcn-connect-out').innerHTML = `<div class="hd-error" role="alert">${esc(e.message)}</div>`;
      } finally { btn.disabled = false; }
    };
  }

  // ---- fleets: many agents of one family, registered by copy-paste scripts
  const FAMILIES = ['claude-code', 'codex', 'pi', 'agent'];
  async function renderFleet() {
    const box = $('hcn-fleet');
    if (!box) return;
    const { rooms } = await api.rooms();
    const usable = rooms.filter((r) => ['owner', 'contributor'].includes(r.role));
    let parents = [];
    try { parents = (await api.agents()).agents.filter((a) => a.status === 'active' && /^pane:/.test(a.instance_id || '')); } catch { /* the form works without */ }
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
          $('hcn-fleet-out').innerHTML = '';
          msg.textContent = 'Requested. They start within a few seconds; progress is listed below.';
          await renderSpawns();
          return;
        }
        if (fd.get('how') === 'direct') showFleetFiles(await api.bulkConnect(body), body);
        else showFleetScript(await api.createJoinToken(body), body, flags);
        await renderAgents();
        await renderJoinTokens();
      } catch (e) { msg.textContent = e.message; msg.setAttribute('role', 'alert'); }
    };
    await renderJoinTokens();
  }

  // ---- hosts that may receive automatic agents, and the progress of fleets started on them
  async function renderHosts() {
    const box = $('hcn-hosts');
    if (!box || !operator) return;
    let hosts;
    try { hosts = (await api.fleetHosts()).hosts; } catch { return; }
    const html = hosts.length ? `<ul class="cx-list">${hosts.map((h) => `<li><span class="grow"><b>${esc(h.host)}</b> ${pill(h.online ? 'online' : 'offline', h.online ? 'var(--status-ok)' : 'var(--text-muted)')}${h.kind === 'local' ? ' <span class="hd-ident">this machine</span>' : ''}${h.online ? ` <span class="hd-ident">${h.families && h.families.length ? esc(h.families.join(', ')) : 'no agent CLIs found'}</span>` : ''}</span><label class="cx-check"><input type="checkbox" data-auto="${esc(h.host)}"${h.auto_agents ? ' checked' : ''}> Automatic agents</label></li>`).join('')}</ul>` : '<div class="hd-empty">No hosts reported yet. The host runner reports every few seconds.</div>';
    if (box.dataset.h === html) return;
    box.dataset.h = html; morph(box, html);
    box.querySelectorAll('[data-auto]').forEach((c) => { c.onchange = async () => { try { await api.setFleetHost(c.dataset.auto, c.checked); } catch (e) { alert(e.message); } renderHosts(); }; });
  }
  async function renderSpawns() {
    const box = $('hcn-spawns');
    if (!box || !operator) return false;
    let list;
    try { list = (await api.spawns()).spawns.slice(0, 8); } catch { return false; }
    const busy = list.some((s) => ['requested', 'claimed'].includes(s.status));
    const colour = { requested: 'var(--status-attention)', claimed: 'var(--accent)', done: 'var(--status-ok)', stalled: 'var(--status-failed)', expired: 'var(--text-muted)' };
    const html = list.length ? `<h3>Recent fleet starts</h3><ul class="cx-list">${list.map((s) => `<li><span class="grow"><b>${esc(s.name_prefix)}-N</b> ${esc(s.harness_type)} × ${s.count}${s.error ? `<br><span class="cx-msg" role="alert">${esc(s.error)}</span>` : ''}${s.placements.length ? `<br>${s.placements.map((p) => `<span class="hd-ident" title="${esc(p.error || '')}">${esc(p.name)} → ${esc(p.host)}${p.status === 'failed' ? ' ✗' : ''}</span>`).join(' ')}` : ''}</span>${pill(s.status === 'done' && s.placements.some((p) => p.status === 'failed') ? 'partly started' : s.status, colour[s.status] || 'var(--text-muted)')}</li>`).join('')}</ul>` : '';
    if (box.dataset.h !== html) { box.dataset.h = html; morph(box, html); }
    return busy;
  }

  const scriptCard = (title, text, note) => `<div class="hd-card cx-stack"><div class="hd-card__title">${esc(title)}</div>
      <div class="cx-code"><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-copy>Copy</button><pre data-text></pre></div>
      <p class="cx-msg">${esc(note)}</p></div>`;
  function fillScripts(out, items) {
    out.innerHTML = items.map((i) => scriptCard(i.title, i.text, i.note)).join('');
    out.querySelectorAll('.hd-card').forEach((card, n) => {
      card.querySelector('[data-text]').textContent = items[n].text;     // textContent: never parsed as HTML
      const btn = card.querySelector('[data-copy]');
      btn.onclick = () => copy(items[n].text, btn);
    });
  }

  function showFleetScript(tok, body, flags = []) {
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
    ]);
  }

  function showFleetFiles(res, body) {
    const files = filesFromBulk(lastCfg.public_url, res.agents);
    const out = $('hcn-fleet-out');
    fillScripts(out, [
      { title: `Created ${res.agents.length} agents${body.room_id ? ' and added them to the room' : ''}`,
        text: `mkdir -p ~/herdr-agents && cd ~/herdr-agents\n${files.map((f) => `cat > ${f.name} <<'EOF'\n${f.text}EOF`).join('\n')}\nchmod 600 *.json`,
        note: 'These credentials are shown once. Paste on the machine that runs the agents, then use worker.sh below.' },
      { title: 'worker.sh', text: workerScript({ base: lastCfg.public_url }), note: 'Save as ~/herdr-agents/worker.sh and chmod +x. Run: worker.sh AGENT.json' },
    ]);
  }

  async function renderJoinTokens() {
    const box = $('hcn-fleet-out');
    if (!box) return;
    let list;
    try { list = (await api.joinTokens()).tokens.filter((t) => t.status === 'active'); } catch { return; }
    let holder = $('hcn-tokens');
    if (!holder) { holder = document.createElement('div'); holder.id = 'hcn-tokens'; box.after(holder); }
    holder.innerHTML = list.length ? `<h3>Active join tokens</h3><ul class="cx-list">${list.map((t) => `<li><span class="grow"><b>${esc(t.name_prefix)}-N</b> ${esc(t.harness_type)} · ${t.uses}/${t.max_uses} used · until ${esc(new Date(t.expires_at * 1000).toLocaleString())}</span><button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-revoke-token="${esc(t.join_id)}">Revoke</button></li>`).join('')}</ul>` : '';
    holder.querySelectorAll('[data-revoke-token]').forEach((b) => { b.onclick = async () => { await api.revokeJoinToken(b.dataset.revokeToken); renderJoinTokens(); }; });
  }

  function showCredential(a) {
    const snippets = connectSnippets(lastCfg, a);
    const out = $('hcn-connect-out');
    out.innerHTML = `<div class="hd-card cx-stack">
      <div class="cx-warn"><strong>Copy this now.</strong> The credential is shown only once and acts as you in the rooms you add this agent to. Keep it private.</div>
      <div class="cx-tabs" role="tablist">${snippets.map((s, i) => `<button class="cx-tab" role="tab" type="button" data-tab="${i}" aria-selected="${i === 0}">${esc(s.name)}</button>`).join('')}</div>
      <div class="cx-code"><button id="hcn-copy" class="hd-btn hd-btn--ghost hd-btn--sm" type="button">Copy</button><pre id="hcn-snippet"></pre></div>
      <p id="hcn-note" class="cx-msg"></p>
      <p class="cx-msg">Next: open a room and add this agent under <em>Your agents in this room</em>.</p></div>`;
    const pick = (i) => {
      $('hcn-snippet').textContent = snippets[i].text;   // textContent: never parsed as HTML
      $('hcn-note').textContent = snippets[i].note;
      out.querySelectorAll('[data-tab]').forEach((t) => t.setAttribute('aria-selected', String(Number(t.dataset.tab) === i)));
      $('hcn-copy').onclick = () => copy(snippets[i].text, $('hcn-copy'));
    };
    out.querySelectorAll('[data-tab]').forEach((t) => { t.onclick = () => pick(Number(t.dataset.tab)); });
    pick(0);
  }

  async function renderAgents() {
    const box = $('hcn-agents');
    if (!box) return;
    const { agents } = await api.agents();
    const live = agents.filter((a) => a.status === 'active');
    // On the dashboard the owner can tie an agent to a running fabric session (the join key is the session id).
    let sessions = null;
    if (operator && fabricSessions) { try { const r = await fabricSessions(); sessions = Array.isArray(r) ? r : (r.sessions || []); } catch { sessions = null; } }
    const sessionOptions = (cur) => {
      const list = [...(sessions || [])];
      if (cur && !list.some((x) => x.session_id === cur)) list.unshift({ session_id: cur, agent_role: 'linked', host_id: '' });
      return list.map((x) => `<option value="${esc(x.session_id)}"${x.session_id === cur ? ' selected' : ''}>${esc(`${x.display_name || x.agent_role || 'agent'} · ${x.host_id || ''} · ${String(x.cwd || x.session_id).split('/').filter(Boolean).pop()}`)}</option>`).join('');
    };
    const knownSessions = new Set((sessions || []).map((session) => session.session_id));
    const families=[...new Set(live.map(a=>a.harness_type).filter(Boolean))].sort();
    box.innerHTML = live.length ? `<div class="cx-agent-toolbar" aria-label="Agent bulk controls"><strong>${live.length} active agents</strong><label class="cx-msg">Group <select data-agent-family-filter><option value="">All families</option>${families.map(f=>`<option value="${esc(f)}">${esc(f)}</option>`).join('')}</select></label><span class="cx-msg" data-agent-selected-count>0 selected</span><button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-agent-stop-selected disabled>Stop selected</button><button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-agent-stop-all>Stop all active</button></div><div class="hd-grid">${live.map((a) => `<div class="hd-card cx-stack cx-agentcard" data-agent-family="${esc(a.harness_type || '')}" data-agent-card="${esc(a.agent_id)}" tabindex="-1"${a.agent_id === linkedAgent ? ' style="outline:2px solid var(--accent);outline-offset:2px"' : ''}>
        <input class="cx-agent-select" type="checkbox" data-agent-select="${esc(a.agent_id)}" aria-label="Select ${esc(a.name || a.agent_id)}">
        <div class="cx-row cx-row--between"><span class="hd-card__title">${agentTitle({ ...a, name: a.name || a.harness_type, display_name: sessions?.find(s=>s.session_id===a.instance_id)?.display_name }, true)}</span>${pill('Active', 'var(--status-ok)')}</div>
        <span class="hd-ident">${esc(a.harness_type)} · ${esc(a.agent_id)}</span>
        ${a.instance_id ? '<span class="hd-card__sub">Linked to a HerdR agent. The readable name is shared; the @ handle routes Collaboration tasks.</span>' : ''}
        ${knownSessions.has(a.instance_id) ? nativeSessionLink(a.instance_id) : ''}
        <span class="hd-card__sub">Expires ${esc(new Date(a.expires_at * 1000).toLocaleDateString())} · ${a.rooms.length ? `in ${esc(a.rooms.map((r) => r.name).join(', '))}` : 'not in any room yet'}</span>
        ${sessions ? `<label class="cx-msg">Fabric agent <select data-link="${esc(a.agent_id)}"><option value="">not linked</option>${sessionOptions(a.instance_id)}</select></label>` : ''}
        <div><button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-revoke="${esc(a.agent_id)}">Stop access</button></div></div>`).join('')}</div>`
      : '<div class="hd-empty">No connected agents yet.</div>';
    if (focusLinkedAgent) {
      const selected = [...box.querySelectorAll('[data-agent-card]')].find((card) => card.dataset.agentCard === linkedAgent);
      if (selected) { selected.focus({ preventScroll: true }); selected.scrollIntoView({ block: 'center' }); focusLinkedAgent = false; }
    }
    wireRename(box, renderAgents);
    box.querySelectorAll('[data-link]').forEach((sel) => { sel.onchange = async () => { try { await api.linkAgent(sel.dataset.link, sel.value); } catch (e) { alert(e.message); } }; });
    box.querySelectorAll('[data-revoke]').forEach((b) => { b.onclick = async () => { if (!window.confirm('Revoke this agent access and stop it?')) return; await api.revokeAgent(b.dataset.revoke); renderAgents(); }; });
    const filter=box.querySelector('[data-agent-family-filter]'),countBox=box.querySelector('[data-agent-selected-count]'),selectedButton=box.querySelector('[data-agent-stop-selected]');
    const syncBulk=()=>{const chosen=[...box.querySelectorAll('[data-agent-select]:checked')];if(countBox)countBox.textContent=`${chosen.length} selected`;if(selectedButton)selectedButton.disabled=!chosen.length;box.querySelectorAll('[data-agent-card]').forEach(card=>{card.hidden=!!(filter?.value&&card.dataset.agentFamily!==filter.value);});};
    filter?.addEventListener('change',syncBulk);box.querySelectorAll('[data-agent-select]').forEach(input=>input.addEventListener('change',()=>{input.closest('[data-agent-card]')?.classList.toggle('cx-agent-card--selected',input.checked);syncBulk();}));
    let stopping = false;
    const stop = async ids => {
      if (stopping || !ids.length || !window.confirm(`Revoke access and stop ${ids.length} agent${ids.length === 1 ? '' : 's'}?`)) return;
      stopping = true;
      const controls = [...box.querySelectorAll('button,input,select')];
      controls.forEach(control => { control.disabled = true; });
      countBox.setAttribute('role', 'status');
      let completed = 0;
      const failures = [];
      for (const id of ids) {
        if (!box.isConnected) break;
        countBox.textContent = `Stopping ${completed + failures.length + 1} of ${ids.length}…`;
        try { await api.revokeAgent(id); completed++; }
        catch (error) { failures.push(`${id}: ${error.message}`); }
      }
      if (!box.isConnected) return;
      try { await renderAgents(); }
      catch (error) { failures.push(`Agent list: ${error.message}`); controls.forEach(control => { control.disabled = false; }); syncBulk(); }
      stopping = false;
      const status = box.querySelector('[data-agent-selected-count]') || countBox;
      status.setAttribute('role', 'status');
      status.textContent = `${completed} stopped${failures.length ? ` · Could not stop: ${failures.join('; ')}. Select remaining agents to retry.` : ''}`;
    };
    selectedButton?.addEventListener('click',()=>stop([...box.querySelectorAll('[data-agent-select]:checked')].map(i=>i.dataset.agentSelect)));
    box.querySelector('[data-agent-stop-all]')?.addEventListener('click',()=>stop(live.map(a=>a.agent_id)));
    syncBulk();
  }

  // ---- one tab per room, then the AI agents page
  let roomsCache = [];
  async function tabsStrip(activeRoom, activePage) {
    try { roomsCache = (await api.rooms()).rooms; onFreshness(true); } catch (e) { onFreshness(false, `Rooms: ${e.message || 'request failed'}`); /* keep the last list */ }
    let waitingCount = 0;
    if (canManagePeople()) { try { waitingCount = (await api.users('pending')).users.length; } catch { /* not an approver */ } }
    const t = (to, label, on, extra = '') => `<button type="button" class="cx-rtab${extra}" role="tab" data-go="${to}" aria-selected="${on}">${label}</button>`;
    return `<div class="cx-rtabs" role="tablist" aria-label="Rooms">${roomsCache.map((r) => t(`/room/${encodeURIComponent(r.room_id)}`, esc(r.name), r.room_id === activeRoom)).join('')}${t('/rooms', '+ New room', activePage === '/rooms', ' cx-rtab--new')}<span class="grow"></span>${canManagePeople() ? t('/people', `People${waitingCount ? ` <span class="cx-badge" title="waiting for approval">${waitingCount}</span>` : ''}`, activePage === '/people') : ''}${t('/agents', 'AI agents', activePage === '/agents')}</div>`;
  }

  const hueOf = (s) => { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
  const initials = (s) => (String(s).replace(/^@/, '').match(/[a-z0-9]/gi) || ['?']).slice(0, 2).join('').toUpperCase();
  const subTab = new Map();                // room id -> the sub-tab last open there

  async function showRoom(roomId) {
    cursor = 0;
    let info;
    try { info = await api.room(roomId); } catch (e) { go('/rooms'); return; }
    const strip = await tabsStrip(roomId, null);
    const canInvite = info.role === 'owner';
    const canPost = ['owner', 'contributor'].includes(info.role);
    const guest = info.role === 'guest';
    let names = new Map((info.members || []).map((m) => [m.user_id, m.email || shortId(m.user_id)]));
    const agentName = new Map();           // agent id -> handle, for rendering task events
    const S = { agents: [], people: [], tasks: [], flow: null, requests: null, mine: [], links: null, missions: null, tabs: null, tabsAt: 0, tabSel: null, externals: null, deleg: null, props: null, propSel: null, propOne: null, profile: null, allow: [], egress: [], helpers: [], paused: false, entries: null };
    const tabs = [['chat', 'Chat'], ...(guest ? [] : [['flow', 'Flow']]), ['tasks', 'Tasks'], ...(guest ? [] : [['agents', 'Agents'], ['tabs', 'Tabs'], ['lineage', 'Lineage'], ['attention', 'Attention'], ['progress', 'Progress'], ['links', 'Workspaces']]), ...(canInvite ? [['proposals', 'Proposals'], ['people', 'People']] : [])];
    let sub = subTab.get(roomId);
    if (!tabs.some(([k]) => k === sub)) sub = 'chat';

    frame('/rooms', `<div class="cx-roomhead"><h1>${esc(info.name)}</h1>${pill(roleLabel(info.role), 'var(--accent)')}</div>
      <div class="cx-stabs" role="tablist" aria-label="${esc(info.name)}">${tabs.map(([k, l]) => `<button type="button" class="cx-stab" role="tab" id="hcn-st-${k}" aria-controls="hcn-p-${k}" data-sub="${k}" data-label="${l}" aria-label="${l}" aria-selected="${k === sub}">${l}<span class="cx-badge" id="hcn-b-${k}" aria-hidden="true" hidden></span></button>`).join('')}</div>
      <section class="hd-card cx-chat cx-panel" id="hcn-p-chat" aria-label="Conversation">
        <div id="hcn-log" class="cx-log" role="log" aria-live="polite"><div class="hd-empty" id="hcn-empty">No messages yet.</div></div>
        <button type="button" id="hcn-jump" class="hd-btn hd-btn--sm cx-jump" hidden>↓ New messages</button>
        ${canPost ? '<div id="hcn-ctx-attached" class="ctx-attached" aria-live="polite"></div><div id="hcn-composer"></div>'
        : '<div class="cx-msg" style="padding:12px">Read-only access.</div>'}
      </section>
      ${guest ? '' : `<section class="hd-card cx-stack cx-panel" id="hcn-p-flow" hidden aria-label="Flow of work"><div class="cx-row cx-row--between"><div class="hd-card__title">Flow of work</div><span class="cx-msg">People and agents; lines are tasks, moving lines are work in progress.</span><button type="button" id="hcn-flow-still" class="hd-btn hd-btn--ghost hd-btn--sm" aria-pressed="false">Pause motion</button></div><div id="hcn-flow"></div><div id="hcn-flow-detail" class="cx-msg" role="status"></div></section>`}
      <section class="cx-panel cx-cols" id="hcn-p-tasks" hidden>
        ${canPost ? '<div class="hd-card cx-stack"><div class="hd-card__title">Give a task</div><form id="hcn-task" class="cx-stack"><label class="cx-field">To<select name="target" id="hcn-task-target"></select></label><label class="cx-field">What should it do?<textarea class="cx-input" name="text" required maxlength="4000" rows="4"></textarea></label><button class="hd-btn" type="submit">Send task</button><span id="hcn-task-msg" class="cx-msg" role="alert"></span></form></div>' : ''}
        <div class="hd-card cx-stack"><div class="hd-card__title">Tasks</div><div id="hcn-tasks"></div></div></section>
      ${guest ? '' : `<section class="cx-panel cx-cols" id="hcn-p-agents" hidden>
        <div class="hd-card cx-stack"><div class="hd-card__title">Agents in this room</div><div id="hcn-roomagents"></div></div>
        ${canInvite ? '<div class="hd-card cx-stack"><div class="hd-card__title">Agent requests</div><div id="hcn-requests"></div></div>' : ''}
        ${canPost ? '<div class="hd-card cx-stack"><div class="hd-card__title">Your agents</div><div id="hcn-grant"></div></div>' : ''}
        ${operator ? '<div class="hd-card cx-stack"><div class="hd-card__title">Helpers</div><div id="hcn-panic"></div><div class="hd-card__title">Host access requests</div><div id="hcn-egress"></div><div class="hd-card__title">External hosts</div><div id="hcn-hosts-table"></div></div>' : ''}
        ${canInvite ? '<div class="hd-card cx-stack"><div class="hd-card__title">Helper profile</div><div id="hcn-profile-box"></div></div>' : ''}</section>`}
      ${guest ? '' : `<section class="hd-card cx-stack cx-panel" id="hcn-p-tabs" hidden aria-label="Primary agents and their subagents"><h2 class="cx-sr">Agents by tab</h2>
        ${canInvite ? `<details class="cx-stack" id="hcn-ext"><summary>Invite a remote agent</summary>
          <form id="hcn-ext-form" class="cx-stack">
            <label class="cx-field">Name shown on its tab<input name="label" required maxlength="40" placeholder="Dana's Claude"></label>
            <fieldset class="cx-stack"><legend class="cx-msg">It may</legend>
              <label><input type="checkbox" checked disabled> read this room's chat</label>
              <label><input type="checkbox" name="post"> post messages</label>
              <label><input type="checkbox" name="lineage"> report its own lineage to the graph</label>
              <label><input type="checkbox" name="proposals"> be given bounded tasks and send artifact proposals (held in quarantine; nothing is applied)</label></fieldset>
            <label class="cx-field">Invite valid for<select name="ttl"><option value="900">15 minutes</option><option value="3600" selected>1 hour</option><option value="86400">1 day</option></select></label>
            <label class="cx-field">Credential lasts<select name="cred"><option value="3600">1 hour</option><option value="86400" selected>1 day</option><option value="604800">7 days</option></select></label>
            <div class="cx-msg">A remote agent never gets a terminal, a shell or any right to give or take tasks. You can revoke it at any time.</div>
            <button class="hd-btn" type="submit">Create invite</button><span id="hcn-ext-msg" class="cx-msg" role="status"></span></form>
          <div id="hcn-ext-code" class="cx-stack" role="status"></div></details>` : ''}
        <div id="hcn-ext-invites"></div>
        <div id="hcn-tabs" aria-live="polite"></div></section>`}
      ${canInvite ? `<section class="cx-panel cx-cols" id="hcn-p-proposals" hidden aria-label="Bounded tasks and proposals">
        <div class="hd-card cx-stack"><div class="hd-card__title">Delegate a bounded task</div>
          <form id="hcn-deleg" class="cx-stack">
            <label class="cx-field">To<select name="target" id="hcn-deleg-target" required></select></label>
            <label class="cx-field">What should it look at?<textarea class="cx-input" name="text" required maxlength="4000" rows="3"></textarea></label>
            <label class="cx-field">Repository paths it may write (one per line, <code>dir/</code> for a folder)<textarea class="cx-input" name="paths" rows="2"></textarea></label>
            <label class="cx-field">Graph nodes it may refer to (one per line, like <code>commit:abc123</code>)<textarea class="cx-input" name="nodes" rows="2"></textarea></label>
            <label class="cx-field">Questions (one per line, <code>id: text</code>)<textarea class="cx-input" name="questions" rows="2"></textarea></label>
            <fieldset class="cx-stack"><legend class="cx-msg">It may answer with</legend>
              <label><input type="checkbox" name="kind" value="doc" checked> a document</label><label><input type="checkbox" name="kind" value="analysis"> an analysis</label>
              <label><input type="checkbox" name="kind" value="patch"> a patch</label><label><input type="checkbox" name="kind" value="link"> a link</label></fieldset>
            <div class="cx-row"><label class="cx-field">Proposals<input name="maxProposals" type="number" min="1" max="5" value="1"></label>
              <label class="cx-field">Steps<input name="maxSteps" type="number" min="1" max="200" value="20"></label>
              <label class="cx-field">Max bytes<input name="maxBytes" type="number" min="256" max="32768" value="16384"></label>
              <label class="cx-field">Expires<select name="expires"><option value="1800">30 minutes</option><option value="3600" selected>1 hour</option><option value="21600">6 hours</option><option value="86400">1 day</option></select></label></div>
            <div class="cx-msg">The agent can only answer with a proposal. It goes to quarantine below; nothing is applied and no command runs. Token limits are advisory: the server cannot measure them.</div>
            <button class="hd-btn" type="submit">Delegate</button><span id="hcn-deleg-msg" class="cx-msg" role="alert"></span></form>
          <div id="hcn-delegs"></div></div>
        <div class="hd-card cx-stack"><div class="hd-card__title">Quarantine</div><div id="hcn-props"></div><div id="hcn-review" aria-live="polite"></div></div></section>` : ''}
      ${guest ? '' : '<section class="hd-card cx-stack cx-panel" id="hcn-p-attention" hidden aria-label="Attention inbox"><div id="hcn-attention" class="cx-stack"></div></section><section class="hd-card cx-stack cx-panel" id="hcn-p-progress" hidden aria-label="Progress and analytics"><h2 class="cx-sr">Progress and analytics</h2><div id="hcn-progress" class="cx-stack"></div><div id="hcn-analytics" class="cx-stack"></div></section>'}
      ${guest ? '' : '<section class="hd-card cx-stack cx-panel" id="hcn-p-lineage" hidden aria-label="Lineage graph, timeline and replay"><h2 class="cx-sr">Lineage</h2><div id="hcn-consults" class="cx-stack"></div><div id="hcn-lineage" class="cx-stack"></div></section>'}
      ${guest ? '' : '<section class="hd-card cx-stack cx-panel" id="hcn-p-links" hidden aria-label="Linked workspaces and missions"><h2 class="cx-sr">Linked workspaces and missions</h2><div id="hcn-links" role="region" aria-live="polite" tabindex="0"></div></section>'}
      ${canInvite ? `<section class="cx-panel cx-cols" id="hcn-p-people" hidden><div class="hd-card cx-stack"><div class="hd-card__title">People</div><div id="hcn-people"></div></div>
        <div class="hd-card cx-stack"><div class="hd-card__title">Invite</div><form id="hcn-invite" class="cx-stack"><label class="cx-field">Invite by email<input name="email" type="email" required placeholder="name@example.org"></label>
          <label class="cx-field">Access<select name="role">${ROLES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}</select></label>
          <button class="hd-btn" type="submit">Send invite</button><span id="hcn-invite-msg" class="cx-msg" role="status"></span></form></div></section>` : ''}`, strip);

    const stillBtn = $('hcn-flow-still');      // WCAG 2.2.2: moving lines run for longer than 5 s, so they can be paused
    if (stillBtn) {
      // A toggle button keeps ONE label and states its state with aria-pressed; changing the label as well would be read twice
      // ("Resume motion, pressed"). The choice is remembered per browser; storage may be blocked, so every access is guarded.
      const KEY = 'hcn.flow.still';
      const apply = (on, save) => {
        stillBtn.setAttribute('aria-pressed', String(on));
        $('hcn-p-flow').classList.toggle('cx-still', on);
        if (save) { try { if (on) window.localStorage.setItem(KEY, '1'); else window.localStorage.removeItem(KEY); } catch { /* storage blocked: the choice lasts until reload */ } }
      };
      let saved = false;
      try { saved = window.localStorage.getItem(KEY) === '1'; } catch { saved = false; }
      if (saved) apply(true, false);
      stillBtn.onclick = () => apply(stillBtn.getAttribute('aria-pressed') !== 'true', true);
    }
    const log = $('hcn-log');
    let alive = true;
    let composer = null;
    let since = -1;
    let ctl = null;
    let wake = null;
    let backoff = 1000;
    const stale = new Set();               // panels whose redraw was held back because you were typing in them
    const onVisible = () => { if (!document.hidden) { since = -1; if (wake) { wake(); wake = null; } } };
    document.addEventListener('visibilitychange', onVisible);
    teardown = () => { alive = false; composer?.dispose(); unbindHelpers(); ctl?.abort(); document.removeEventListener('visibilitychange', onVisible); if (wake) wake(); };
    const kick = () => { since = -1; ctl?.abort(); };       // an action just happened: refetch now

    // Replace a container's markup only when it changed and you are not typing in it.
    const put = (id, html) => {
      const el = $(id);
      if (!el || el.dataset.h === html) return false;
      if (el.contains(document.activeElement) && document.activeElement.matches('input,textarea,select')) { stale.add(id); return false; }
      const had = el.contains(document.activeElement) && document.activeElement !== el ? document.activeElement : null;
      const again = had ? stableSelector(had) : null;
      el.dataset.h = html; stale.delete(id);
      // Morphed in place: unchanged controls keep their nodes (and any focus/scroll); a focused
      // control that left the new markup is handed to its equivalent (stable selector) or parked on
      // the panel, so focus never drops to <body>.
      const stranded = morph(el, html);
      handOff(stranded, (node) => {
        if (!(had && node.contains(had))) return null;
        let target = again ? el.querySelector(again) : null;
        if (!target) { if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '-1'); target = el; }
        return target;
      });
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
        const remote = e.sender.type === 'external';
        const agent = e.sender.type === 'agent' || remote;
        const mine = !agent && e.sender.id === me.user_id;
        const remoteRow = remote ? ((S.externals && S.externals.externals) || []).find((r) => r.agent_id === e.sender.id) : null;
        const who = remote ? `Remote: ${remoteRow ? remoteRow.label : 'agent invited by ' + (names.get(e.sender.owner_user_id) || shortId(e.sender.owner_user_id))}` : agent ? (agentName.has(e.sender.id) ? `@${agentName.get(e.sender.id)}` : `Agent of ${names.get(e.sender.owner_user_id) || shortId(e.sender.owner_user_id)}`)
          : (mine ? 'You' : (names.get(e.sender.id) || shortId(e.sender.id)));
        const key = `${e.sender.type}:${e.sender.id}`;
        const cont = key === lastKey && at - lastAt < 5 * 60000;
        lastKey = key; lastAt = at.getTime();
        row.className = `cx-m${mine ? ' cx-m--mine' : ''}${agent ? ' cx-m--agent' : ''}${cont ? ' cx-m--cont' : ''}`;
        row.innerHTML = `${mine ? '' : `<span class="cx-av" style="--h:${hueOf(key)}" aria-hidden="true">${agent ? '🤖' : esc(initials(who))}</span>`}<div class="cx-mbody"><div class="cx-meta"><b>${esc(who)}</b><span>${esc(time)}</span></div><div class="cx-bubble conversation-markdown">${mentionChips(renderMarkdown(e.text), e.mentions, mentionCtx())}</div>${e.context ? `<div class="ctx-ref">${renderContextChip(e.context)}</div>` : ''}</div>`;
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
      // The first batch is history and appears at once; later rows arrived live and slide in. A task
      // status change reuses the flow graph's one-shot cx-flow-pulse glow. Calm mode: neither.
      const live = !!log.querySelector('.cx-m, .cx-taskcard, .cx-event');
      const frag = document.createDocumentFragment();
      const rows = shown.map((e) => {
        const row = eventRow(e);
        if (live && e.event_type === 'task_updated' && !isCalm()) row.classList.add('cx-flow-pulse');
        frag.appendChild(row);
        return row;
      });
      log.appendChild(frag);
      if (live) rows.forEach(enter);
      if (stick) log.scrollTop = log.scrollHeight; else $('hcn-jump').hidden = false;
    };
    log.addEventListener('scroll', () => { if (log.scrollTop + log.clientHeight >= log.scrollHeight - 60) $('hcn-jump').hidden = true; });
    $('hcn-jump').onclick = () => { log.scrollTop = log.scrollHeight; };

    // ---- panels, each drawn from the last state only while it is on screen
    const permPills = (actions) => actions.map((a) => pill(PERM_SHORT[a] || a, 'var(--status-ok)')).join(' ');
    const taskChip = (st) => pill(st, ({ done: 'var(--status-ok)', failed: 'var(--status-failed)', pending: 'var(--status-attention)', accepted: 'var(--accent)' })[st] || 'var(--text-muted)');
    const ttlSelect = () => '<label class="cx-msg">Lasts <select name="ttl"><option value="">30 days (default)</option><option value="3600">1 hour</option><option value="86400">1 day</option><option value="604800">7 days</option><option value="7776000">90 days</option></select></label>';
    const expiryOf = (f) => { const v = Number(f.querySelector('select[name="ttl"]')?.value || 0); return v ? Date.now() / 1000 + v : undefined; };
    const warn = (e) => { alert(e.message); };
    const act = async (fn) => { try { await fn(); } catch (e) { warn(e); } kick(); };

    const namedAgent = a => ({...a,display_name:S.flow?.nodes?.find(n=>n.agent_id===a.agent_id)?.fabric?.display_name});
    const drawRoomAgents = () => {
      const mine = new Set(S.mine.map((a) => a.agent_id));
      const html = S.agents.length ? `<ul class="cx-list">${S.agents.map((a) => `<li class="cx-agentrow"><span class="grow">${agentTitle(namedAgent(a), mine.has(a.agent_id) || operator)}<span class="cx-sub1">${esc(a.harness_type)} · ${esc(names.get(a.owner_user_id) || shortId(a.owner_user_id))}</span></span><span class="cx-pills">${permPills(a.actions)}</span>${(canInvite || mine.has(a.agent_id)) ? `<button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-rmagent="${esc(a.agent_id)}">Remove</button>` : ''}</li>`).join('')}</ul>` : '<div class="hd-empty">No agents in this room yet.</div>';
      if (put('hcn-roomagents', html)) { $('hcn-roomagents').querySelectorAll('[data-rmagent]').forEach((b) => { b.onclick = () => act(() => api.removeAgent(roomId, b.dataset.rmagent)); }); wireRename($('hcn-roomagents'), redraw('hcn-roomagents')); }
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
      box.querySelectorAll('[data-go]').forEach((el) => { el.onclick = (e) => { if (!standalone) { e.preventDefault(); go(el.dataset.go); } }; });
      wireRename(box, redraw('hcn-grant'));
      box.querySelectorAll('form[data-agent]').forEach((f) => {
        f.onsubmit = (ev) => {
          ev.preventDefault();
          const actions = picked(f, 'p');
          if (actions.length) act(() => api.grantAgent(roomId, f.dataset.agent, actions, expiryOf(f)));
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
          act(() => api.approveAll(roomId, { request_ids: requests.map((r) => r.request_id), ...(v ? { expires_at: Date.now() / 1000 + v } : {}) }));
        };
      }
      box.querySelectorAll('form[data-req]').forEach((f, i) => {
        const asked = requests[i].actions;
        f.querySelectorAll('input[name="p"]').forEach((c) => { if (!asked.includes(c.value)) { c.disabled = true; c.checked = false; } });   // cannot approve more than was asked
        f.onsubmit = (ev) => {
          ev.preventDefault();
          const actions = picked(f, 'p');
          if (actions.length) act(() => api.decideRequest(roomId, f.dataset.req, 'approve', actions, expiryOf(f)));
        };
        f.querySelector('[data-deny]').onclick = () => act(() => api.decideRequest(roomId, f.dataset.req, 'deny'));
      });
    };

    // ---- agent helpers: panic switch, host-access requests and hosts (operator), profile (owner)
    const loadHelpers = async () => {
      if (operator) {
        try { S.egress = ((await api.egressRequests()).requests || []).filter((r) => r.room_id === roomId); } catch { /* keep the old list */ }
        try { const h = await api.helpers(); S.helpers = h.helpers || []; S.paused = Boolean(h.paused); } catch { /* keep */ }
      } else { try { S.helpers = (await api.myHelpers()).helpers || []; } catch { /* guests and old servers: no helper list */ } }
      if (operator) { try { const x = await api.egressAllowlist(roomId); S.allow = x.allowlist || []; S.entries = Array.isArray(x.entries) ? x.entries : null; } catch { /* keep */ } }
      if (canInvite) { try { S.profile = await api.roomProfile(roomId); } catch { /* not readable: keep */ } }
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
      api, roomId, operator, owner: canInvite, act, getProfile: () => S.profile, setProfile: (p) => { S.profile = p; $('hcn-profile-msg').textContent = 'Saved.'; },
      onError: (m) => { $('hcn-profile-msg').textContent = m; },
      refresh: async () => { await loadHelpers(); drawHelpers(); },
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

    let peopleBusy = false;
    const drawPeople = async () => {
      if (!canInvite || peopleBusy) return;
      peopleBusy = true;
      try {
        const [room, inv] = await Promise.all([api.room(roomId), api.invites(roomId)]);
        names = new Map((room.members || []).map((m) => [m.user_id, m.email || shortId(m.user_id)]));
        const html = `<ul class="cx-list">${room.members.map((m) => `<li><span class="grow">${esc(m.email || shortId(m.user_id))}</span>${pill(roleLabel(m.role), m.role === 'owner' ? 'var(--accent)' : 'var(--status-active)')}${m.role !== 'owner' ? `<button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-rm="${esc(m.user_id)}">Remove</button>` : ''}</li>`).join('')}
          ${inv.invites.map((i) => `<li><span class="grow">${esc(i.email)}</span>${pill('Invited', 'var(--status-attention)')}<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-cancel="${esc(i.invite_id)}">Cancel</button></li>`).join('')}</ul>`;
        if (put('hcn-people', html)) {
          $('hcn-people').querySelectorAll('[data-rm]').forEach((b) => { b.onclick = () => act(async () => { await api.removeMember(roomId, b.dataset.rm); $('hcn-people').dataset.h = ''; }); });
          $('hcn-people').querySelectorAll('[data-cancel]').forEach((b) => { b.onclick = () => act(async () => { await api.cancelInvite(roomId, b.dataset.cancel); $('hcn-people').dataset.h = ''; }); });
        }
      } catch { /* keep what is shown */ } finally { peopleBusy = false; }
    };

    // Tabs and panes come from recorded spawn lineage the server lets this person read in THIS room (room scope
    // plus its linked missions). A failed or empty read just shows the empty state; nothing is guessed.
    let tabsBusy = false;
    const loadTabs = async () => {
      if (tabsBusy || Date.now() - S.tabsAt < 8000) return;
      tabsBusy = true;
      try {
        const none = { nodes: [], edges: [] };
        const [agents, tabNodes, paneNodes] = await Promise.all([
          api.graphList(roomId, 'agent', 'spawn'), api.graphList(roomId, 'tab'), api.graphList(roomId, 'pane')].map((p) => p.catch(() => none)));
        // The remote-agent roster comes from the collaboration service itself (so it works where the graph is
        // not served); a failed read keeps the last roster rather than inventing one.
        try { S.externals = await api.externals(roomId); } catch { /* keep the last roster */ }
        S.tabs = buildTabsPanes([agents, tabNodes, paneNodes], S.links || [], S.externals ? S.externals.externals : null);
      } finally { S.tabsAt = Date.now(); tabsBusy = false; }
    };
    function drawTabs() {
      const show = () => {
        const box = $('hcn-tabs');
        if (!box) return;
        const pend = (S.externals && S.externals.invites) || [];
        put('hcn-ext-invites', pend.length ? `<ul class="cx-list" aria-label="Open remote-agent invites">${pend.map((i) => `<li><span class="grow"><b>${esc(i.label)}</b> <span class="cx-msg">invited, not yet used · may ${esc((i.actions || []).join(', '))}${i.lineage ? ', report lineage' : ''}</span></span>${pill('Invited', 'var(--status-attention)')}<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-cancel-ext="${esc(i.invite_id)}">Cancel</button></li>`).join('')}</ul>` : '');
        $('hcn-ext-invites')?.querySelectorAll('[data-cancel-ext]').forEach((b) => { b.onclick = () => act(async () => { await api.revokeExternalInvite(roomId, b.dataset.cancelExt); S.tabsAt = 0; $('hcn-ext-invites').dataset.h = ''; }); });
        if (put('hcn-tabs', renderTabsPanes(S.tabs || { tabs: [], hidden_by_grant: 0 }, S.tabSel, { canManage: canInvite }))) {
          const ids = (S.tabs?.tabs || []).map((t) => t.agent_id);
          box.querySelectorAll('[data-revoke]').forEach((b) => {
            b.onclick = () => {
              if (!window.confirm('Revoke this remote agent? It is cut off at once; what it already did stays visible.')) return;
              act(async () => { await api.revokeExternal(roomId, b.dataset.revoke); S.tabsAt = 0; box.dataset.h = ''; });
            };
          });
          box.querySelectorAll('[data-tab]').forEach((b) => {
            b.onclick = () => { S.tabSel = b.dataset.tab; box.dataset.h = ''; drawTabs(); };
            b.onkeydown = (e) => {
              const to = nextTab(ids, b.dataset.tab, e.key);
              if (to) { e.preventDefault(); S.tabSel = to; box.dataset.h = ''; drawTabs(); box.querySelector(`[data-tab="${CSS.escape(to)}"]`)?.focus(); }
            };
          });
        }
      };
      show();
      loadTabs().then(show);
    }
    // Bounded tasks and proposals (T11): the owner's quarantine view. Content is untrusted text; see collab-proposals.js.
    let propBusy = false;
    const loadProposals = async () => {
      if (propBusy) return;
      propBusy = true;
      try {
        const [d, p] = await Promise.all([api.delegations(roomId), api.proposals(roomId)]);
        S.deleg = d; S.props = p;
        if (S.propSel) { try { S.propOne = await api.proposal(roomId, S.propSel); } catch { S.propOne = null; } }
      } catch { /* keep what is shown; a service with delegation off answers 403 */ } finally { propBusy = false; }
    };
    function drawProposals() {
      const show = () => {
        const targets = ((S.externals && S.externals.externals) || []).filter((x) => x.proposals && x.state === 'active');
        put('hcn-deleg-target', targets.length ? targets.map((x) => `<option value="${esc(x.agent_id)}">${esc(x.label)}</option>`).join('') : '<option value="">No remote agent was invited for bounded tasks</option>');
        if (put('hcn-delegs', renderDelegations(S.deleg))) {
          $('hcn-delegs').querySelectorAll('[data-cancel-deleg]').forEach((b) => { b.onclick = () => act(async () => { await api.cancelDelegation(roomId, b.dataset.cancelDeleg); $('hcn-delegs').dataset.h = ''; }); });
        }
        if (put('hcn-props', renderProposalList(S.props, S.propSel))) {
          $('hcn-props').querySelectorAll('[data-proposal]').forEach((b) => { b.onclick = () => { S.propSel = b.dataset.proposal; $('hcn-props').dataset.h = ''; $('hcn-review').dataset.h = ''; loadProposals().then(show); }; });
        }
        if (put('hcn-review', renderReview(S.propOne))) {
          $('hcn-review').querySelectorAll('[data-decide]').forEach((b) => {
            b.onclick = () => act(async () => {
              const ask = b.dataset.decide === 'accept' ? 'Record this proposal in the graph as accepted? Nothing is applied or run.' : 'Reject this proposal?';
              if (!window.confirm(ask)) return;
              await api.decideProposal(roomId, b.dataset.pid, decisionBody(b.dataset.decide, b.dataset.digest, ($('hcn-prop-reason') || {}).value));
              ['hcn-props', 'hcn-review', 'hcn-delegs'].forEach((id) => { $(id).dataset.h = ''; });
              S.focusProposal = b.dataset.pid;          // the decide buttons go away: focus moves on to this proposal's row, not to <body>
              await loadProposals();
            });
          });
        }
        landOnDecided();
      };
      show();
      loadTabs().then(() => loadProposals()).then(show);
    }
    // After a decision the review buttons are gone: land on the decided proposal's own row (its logical successor).
    const landOnDecided = () => {
      if (!S.focusProposal) return;
      const row = $('hcn-props') && $('hcn-props').querySelector(`[data-proposal="${CSS.escape(S.focusProposal)}"]`);
      if (row) { row.focus({ preventScroll: true }); S.focusProposal = null; }
    };

    // ---- Lineage: graph, Gantt, replay and inspector. Read-only; the server filters every read by your CURRENT
    // membership. Live = the head of your ledger (refreshed while open); a replay position is static.
    // Comments and steering for the selected node (collab-workbench.js). The server checks every one of these again.
    const WB = { at: 0, edge: null, nodeId: null, comments: [], target: null, forks: null, forksError: null, steers: [], error: null, steerError: null, notice: null, draft: freshDraft(),
      editing: null, history: null, confirming: null, busy: false, seq: 0 };
    const wbModel = () => ({ ...WB, me: me.user_id, isOwner: info.role === 'owner',
      writable: canPost && (LIN.step === null || !!(LIN.asOf && LIN.asOf.head)),
      readOnlyReason: canPost ? 'A replay shows a past state. Choose Live to comment or propose a change.' : 'Your role in this room is read only.' });
    // ---- T14: contextual chat, questions to a person or remote agent, and links across workspaces. Every one of these
    // is checked again by the server; a hidden reference is shown as one fixed phrase and carries nothing.
    const CTX = { attach: null, consults: [], links: [], targets: [], features: { context_refs: false, consultation: false, cross_links: false },
      at: 0, busy: false, error: null, notice: null, askDraft: { target: '', question: '' }, seq: 0 };
    const ctxModel = () => {
      const f = CTX.features;
      return { nodeId: LIN.sel, enabled: f.context_refs || f.consultation || f.cross_links, chat: f.context_refs, consult: f.consultation, crossLinks: f.cross_links,
        canPost, me: me.user_id, isOwner: info.role === 'owner', consults: CTX.consults, links: CTX.links, busy: CTX.busy, error: CTX.error, notice: CTX.notice,
        people: CTX.targets.map((t) => ({ kind: t.kind, id: t.id, label: t.kind === 'agent' ? t.label : (names.get(t.id) || shortId(t.id)) })),
        otherNodes: mergePages(LIN.pages).nodes.map((n) => ({ id: n.id, label: n.label })),
        askDraft: CTX.askDraft };
    };
    function drawAttach() {
      const h = $('hcn-ctx-attached');
      if (!h) return;
      morph(h, CTX.attach ? `<span class="ctx-chip"><span aria-hidden="true">◆</span> The next message refers to ${esc(CTX.attach)} <button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-ctx-act="detach">Remove</button></span>` : '');
      h.onclick = (e) => { if (e.target.closest('[data-ctx-act="detach"]')) { CTX.attach = null; drawAttach(); } };
    }
    async function loadContext() {
      if (CTX.busy) return;
      const my = ++CTX.seq;
      const [c, l] = await Promise.allSettled([api.consultations(roomId), api.xlinks(roomId)]);
      if (my !== CTX.seq || !alive) return;
      CTX.at = Date.now();
      if (c.status === 'fulfilled') { CTX.consults = Array.isArray(c.value.consultations) ? c.value.consultations : []; CTX.targets = Array.isArray(c.value.targets) ? c.value.targets : []; if (c.value.features) CTX.features = c.value.features; }
      if (l.status === 'fulfilled') { CTX.links = Array.isArray(l.value.links) ? l.value.links : []; if (l.value.features) CTX.features = l.value.features; }
      drawLineage(true);
    }
    async function ctxRun(fn, done) {
      CTX.busy = true; CTX.error = null; CTX.notice = null; drawLineage(true);
      try { await fn(); CTX.notice = done || null; } catch (e) { CTX.error = e instanceof AuthError ? 'Your session expired. Sign in again.' : (e && e.message) || 'The request failed.'; }
      CTX.busy = false; CTX.at = 0; await loadContext();
    }
    const ctxKey = () => `web-ctx-${Date.now().toString(36)}-${randomString(9)}`;
    function ctxAct(act, id) {
      if (act === 'attach' && LIN.sel) { CTX.attach = LIN.sel; CTX.notice = 'The next message you send in the Chat tab refers to this item.'; drawAttach(); drawLineage(true); }
      else if (act === 'cancel') ctxRun(() => api.cancelConsultation(roomId, id), 'Question cancelled.');
      else if (act === 'decline') ctxRun(() => api.answerConsultation(roomId, id, declineBody()), 'Declined.');
      else if (act === 'revoke') ctxRun(() => api.revokeLink(roomId, id), 'Link revoked.');
    }
    function ctxSubmit(kind, id, form) {
      const node = LIN.sel;
      if (kind === 'ask' && node) {
        const q = String(form.get('question') || '').trim(); const t = String(form.get('target') || '');
        if (q && t) ctxRun(async () => { await api.askConsultation(roomId, askBody(node, t, q, ctxKey())); CTX.askDraft = { target: t, question: '' }; }, 'Asked. You will see the answer here.');
      } else if (kind === 'answer') {
        const a = String(form.get('answer') || '').trim();
        if (a) ctxRun(() => api.answerConsultation(roomId, id, answerBody(a)), 'Answered.');
      } else if (kind === 'link' && node) {
        const to = String(form.get('to') || '').trim();
        if (to) ctxRun(() => api.createLink(roomId, linkBody(node, to, form.get('note'), ctxKey())), 'Linked.');
      }
    }
    function drawConsults() {
      const h = $('hcn-consults');
      if (!h) return;
      if (!h.dataset.bound) {                                     // the inbox sits outside the lineage box: bind it once
        h.dataset.bound = '1';
        h.addEventListener('click', (e) => {
          const open = e.target.closest('[data-ctx-open]');
          if (open) { pickNode(open.dataset.ctxOpen); drawLineage(true); return; }
          const cx = e.target.closest('[data-ctx-act]');
          if (cx) ctxAct(cx.dataset.ctxAct, cx.dataset.ctxId);
        });
        h.addEventListener('submit', (e) => {
          e.preventDefault();
          const cf = e.target.closest && e.target.closest('[data-ctx-form]');
          if (cf && !CTX.busy) ctxSubmit(cf.dataset.ctxForm, cf.dataset.ctxId, new FormData(cf));
        });
      }
      const html = renderInbox({ ...ctxModel(), enabled: CTX.features.consultation });
      if (h.dataset.h !== html) { h.dataset.h = html; handOff(morph(h, html), () => null); }
    }
    // layout preferences (view, mode, clock) live in this browser only, apart from anything the server holds about the mission
    const PREF_KEY = 'hcn.lineage.prefs';
    const readPrefs = () => { try { const p = JSON.parse(window.localStorage.getItem(PREF_KEY) || '{}'); return { view: p.view === 'gantt' ? 'gantt' : 'graph', mode: p.mode === 'table' ? 'table' : 'visual', clock: p.clock === 'reported' ? 'reported' : 'recorded' }; } catch { return {}; } };
    const savePrefs = () => { try { window.localStorage.setItem(PREF_KEY, JSON.stringify({ view: LIN.view, mode: LIN.mode, clock: LIN.clock })); } catch { /* storage may be off: the layout is just not remembered */ } };
    const LIN = { vp: null, focusId: null, vpW: 0, vpH: 0, step: null, view: 'graph', mode: 'visual', clock: 'recorded', sel: null, focus: null, pages: [], total: 0, next: null,
      truncated: null, asOf: null, timeline: null, error: null, timelineError: null, html: '', pending: null, fails: 0, notice: null, busy: false, at: 0, seq: 0, bound: false, layout: null,
      filters: { q: '', types: new Set(), edgeTypes: new Set(), state: '' } };
    Object.assign(LIN, readPrefs());
    // The terminal link: only the operator on the dashboard, only for a session the dashboard itself lists. It goes to the dashboard's
    // session page, whose Terminal button is the existing authorized route; nothing here builds or sends a command.
    const SESS = { ids: null, at: 0 };
    const refreshSessions = () => {
      if (!operator || standalone || !fabricSessions || Date.now() - SESS.at < 30000) return;
      SESS.at = Date.now();
      Promise.resolve().then(fabricSessions).then((r) => { SESS.ids = new Set((Array.isArray(r) ? r : (r && r.sessions) || []).map((x) => x.session_id)); if (alive && LIN.sel) drawLineage(true); }).catch(() => { SESS.ids = null; });
    };
    const terminalFor = (id) => {
      const tail = typeof id === 'string' && id.startsWith('session:') ? id.slice(8) : null;
      return tail && SESS.ids && SESS.ids.has(tail) ? { href: `/#/herd/session/${encodeURIComponent(tail)}` } : null;
    };
    // Operator-only: the server is asked (the real authorize route) whether the operator may use the terminal of the workspace this node belongs to.
    // Nothing is rendered for anyone else, so there is also no handler to reach: the click path re-checks the same condition.
    const TERM = { result: null, busy: false };
    const termAuthFor = (id) => {
      if (!operator || standalone || !id) return null;
      const node = mergePages(LIN.pages).nodes.find((n) => n.id === id);
      const link = workspaceLinkFor(S.links, node);
      return link && link.can_terminal === true ? { link, result: TERM.result, busy: TERM.busy } : null;
    };
    async function termAuth(linkId) {
      const t = termAuthFor(LIN.sel);
      if (!t || t.link.link_id !== linkId || TERM.busy) return;                  // only the control that is on screen, only for the operator
      TERM.busy = true; TERM.result = null; drawLineage(true);
      try { await api.authorizeWorkspace(roomId, linkId, 'terminal'); TERM.result = { link_id: linkId, ok: true }; }
      catch (e) { TERM.result = { link_id: linkId, ok: false, reason: e && e.status ? `HTTP ${e.status}` : 'forbidden' }; }
      TERM.busy = false;
      if (alive) drawLineage(true);
    }
    const vpSize = () => {
      const box = $('hcn-lineage');
      // a hidden panel measures 0: fall back to the window, never to a desktop width on a phone
      const total = (box && box.clientWidth) || Math.max(280, window.innerWidth - 32);
      const side = total >= 760 ? 336 : 0;            // the inspector sits beside the graph from 761px (collab.css), below it under 760
      const w = Math.min(1200, Math.max(280, Math.floor((total || 960) - side)));
      return { w, h: w < 520 ? 360 : VIEW_H };
    };
    const linModel = () => {
      const graphAll = mergePages(LIN.pages);
      const found = filterGraph(graphAll, LIN.filters);
      const filtered = LIN.focusId && found.nodes.some((n) => n.id === LIN.focusId) ? { ...found, ...focusSubgraph(found, LIN.focusId) } : found;
      const layout = LIN.mode !== 'table' ? layoutCached(filtered) : null;
      const max = vpSize();
      const sz = viewportSize(layout, max.w, max.h);
      LIN.vpW = sz.w; LIN.vpH = sz.h;
      if (LIN.vp && layout) LIN.vp = clampView(LIN.vp, layout, sz.w, sz.h);
      return { graphAll, filtered, vp: LIN.vp, focusId: LIN.focusId, vpW: sz.w, vpH: sz.h, terminal: terminalFor(LIN.sel), termAuth: termAuthFor(LIN.sel), asOf: LIN.asOf, timeline: LIN.timeline, error: LIN.error, timelineError: LIN.timelineError, queued: LIN.pending !== null, notice: LIN.notice, busy: LIN.busy, view: LIN.view,
        mode: LIN.mode, clock: LIN.clock, sel: LIN.sel, focus: LIN.focus, filters: LIN.filters, filtersOpen: LIN.filtersOpen ?? window.innerWidth > 760, total: LIN.total,
        workbench: LIN.sel ? renderWorkbench(wbModel()) + renderContextPanel(ctxModel()) : '',
        graphTruncated: LIN.next !== null ? (LIN.truncated || 'limit') : (LIN.truncated || null), graphMore: LIN.next !== null, width: Math.max(640, ($('hcn-lineage')?.clientWidth || 900)) };
    };
    async function loadWorkbench(keep = false) {
      const node = LIN.sel;
      if (!node) { WB.nodeId = null; return; }
      const my = ++WB.seq;
      WB.nodeId = node; WB.at = Date.now();
      const forkable = /^(attempt|agent|task|approach):/.test(node);
      const [c, t, st, fk] = await Promise.allSettled([api.comments(roomId, { anchor: WB.edge ? { kind: 'edge', ...WB.edge } : { kind: 'node', node_id: node } }), api.steeringTarget(roomId, node), api.steeringList(roomId, node),
        forkable ? api.forks(roomId, node) : Promise.reject(new Error('not forkable'))]);
      if (my !== WB.seq || !alive || LIN.sel !== node) return;
      WB.comments = c.status === 'fulfilled' && Array.isArray(c.value.comments) ? c.value.comments : [];
      if (c.status === 'rejected') WB.error = 'Comments could not be loaded for this item.'; else if (!keep) WB.error = null;
      WB.target = t.status === 'fulfilled' && t.value && typeof t.value.fence === 'string' ? t.value : null;
      WB.steers = st.status === 'fulfilled' && Array.isArray(st.value.steering) ? st.value.steering : [];
      WB.forks = fk.status === 'fulfilled' && fk.value && Array.isArray(fk.value.forks) ? fk.value : null;       // 403 forks_disabled / 404 simply show nothing
      WB.forksError = forkable && fk.status === 'rejected' && fk.reason && fk.reason.status && ![403, 404].includes(fk.reason.status) ? 'Forks could not be loaded for this item.' : null;
      const mine = WB.forks && WB.forks.forks.find((x) => x.approach_id === node);
      if (mine && mine.head_sha) WB.draft.expected_head = mine.head_sha;
      const bc = WB.target && WB.target.fork ? Object.keys(WB.target.fork.base_candidates || {}) : [];
      if (bc.length && !bc.includes(WB.draft.base_sha)) WB.draft.base_sha = bc[0];
      if (t.status === 'rejected') WB.steerError = 'Steering details could not be loaded for this item.'; else if (!keep) WB.steerError = null;
      drawLineage(true);
    }
    function pickNode(id) {
      LIN.sel = id; LIN.focus = id;
      if (!id) { WB.nodeId = null; WB.seq += 1; return; }
      if (WB.nodeId !== id) {
        Object.assign(WB, { nodeId: id, edge: null, comments: [], target: null, forks: null, forksError: null, steers: [], error: null, steerError: null, notice: null, editing: null, history: null, confirming: null, draft: freshDraft() });
        loadWorkbench();
      }
    }
    const wbWhy = (e, fallback) => (e instanceof AuthError ? 'Your session expired. Sign in again.' : e && e.code === 'stale_fence' ? 'That item changed since you read it. It was reloaded: check it and try again.' : (e && e.message) || fallback);
    async function wbRun(fn, { steer = false, done = null } = {}) {
      WB.busy = true; WB.error = null; WB.steerError = null; WB.notice = null; drawLineage(true);
      try { await fn(); if (done) WB.notice = done; } catch (e) {
        if (steer) WB.steerError = wbWhy(e, 'The request failed.'); else WB.error = wbWhy(e, 'The request failed.');
      } finally { WB.busy = false; if (alive) drawLineage(true); }
      await loadWorkbench(true);                                      // refresh the lists but keep the message just shown
    }
    const wbKey = () => `web-wb-${Date.now().toString(36)}-${randomString(9)}`;
    function wbAct(act, id) {
      const c = WB.comments.find((x) => x.comment_id === id);
      const s = WB.steers.find((x) => x.steering_id === id);
      if (act === 'pick-fork' && id) { pickNode(id); drawLineage(true); return; }
      if (act === 'promote-start' && id) { pickNode(id); WB.draft.action = 'promote'; drawLineage(true); return; }
      if (act === 'edge-clear') { WB.edge = null; WB.editing = null; WB.history = null; loadWorkbench(); return; }
      if (act === 'edit' && c) { WB.editing = { id, text: c.text || '' }; drawLineage(true); }
      else if (act === 'edit-cancel') { WB.editing = null; drawLineage(true); }
      else if (act === 'retract' && c) wbRun(() => api.retractComment(roomId, id, c.revision), { done: 'Comment retracted. Its history is kept.' });
      else if (act === 'history' && c) {
        if (WB.history && WB.history.id === id) { WB.history = null; drawLineage(true); return; }
        api.commentHistory(roomId, id).then((h) => { WB.history = { id, items: h.comment.history }; drawLineage(true); }).catch((e) => { WB.error = wbWhy(e, 'History could not be loaded.'); drawLineage(true); });
      } else if (act === 'confirm' && s) { WB.confirming = id; WB.steerError = null; drawLineage(true); }
      else if (act === 'confirm-cancel') { WB.confirming = null; drawLineage(true); }
      else if (act === 'confirm-go' && s) {
        wbRun(async () => {
          const fresh = await api.steeringTarget(roomId, s.target.node_id);      // confirm only against what you can see NOW
          if (fresh.fence !== s.target.fence) { const err = new Error('The target changed after this was proposed, so nothing was sent. Ask for a new proposal.'); err.code = 'stale_fence'; throw err; }
          await api.confirmSteering(roomId, id, { digest: s.digest, expected_fence: fresh.fence });
          WB.confirming = null;
        }, { steer: true, done: 'Confirmed. It is delivered once, to the source that reported this item.' });
      } else if (act === 'reject' && s) wbRun(() => api.rejectSteering(roomId, id), { steer: true, done: 'Request closed.' });
    }
    function wbSubmit(kind, id, form) {
      const node = WB.nodeId;
      if (!node) return;
      if (kind === 'comment') {
        const t = String(form.get('text') || '').trim();
        if (t) wbRun(async () => { await api.addComment(roomId, commentBody(WB.edge ? { kind: 'edge', ...WB.edge } : { kind: 'node', node_id: node }, t, wbKey())); WB.draft.comment = ''; }, { done: 'Comment posted.' });
      } else if (kind === 'edit' && WB.editing) {
        const c = WB.comments.find((x) => x.comment_id === id);
        const t = String(form.get('text') || '').trim();
        if (c && t) wbRun(async () => { await api.editComment(roomId, id, t, c.revision); WB.editing = null; }, { done: 'Comment updated. The earlier text stays in its history.' });
      } else if (kind === 'propose' && WB.target) {
        wbRun(async () => { await api.proposeSteering(roomId, proposalBody(node, WB.draft, WB.target, wbKey())); WB.draft = freshDraft(); },
          { steer: true, done: 'Proposed. A room owner must confirm it before anything is sent.' });
      }
    }
    const linFail = (e) => (/failed|404|not found/i.test(String(e && e.message)) ? 'No lineage is available for this room.' : 'Could not load the lineage. Press Retry, or choose Live.');
    async function loadLineage(more = false) {
      const my = ++LIN.seq;
      let again = false;
      LIN.busy = true; drawLineage(true);
      try {
        const g = await api.lineage(roomId, { step: LIN.step, offset: more ? LIN.next : 0 });
        if (my !== LIN.seq || !alive) return;
        let t = null;
        LIN.timelineError = null;
        if (LIN.view === 'gantt' || LIN.timeline) {
          // the timeline is read at the position the graph answered for, so the two never disagree
          try { t = await api.lineageTimeline(roomId, { step: g.as_of.head ? null : g.as_of.step }); } catch (e) { LIN.timelineError = 'The timeline could not be loaded.'; }
          if (my !== LIN.seq || !alive) return;
        }
        LIN.pages = more ? [...LIN.pages, g] : [g];
        LIN.total = g.total_nodes; LIN.next = g.next_offset; LIN.truncated = pagesTruncated(LIN.pages); LIN.asOf = g.as_of;
        if (t) LIN.timeline = t;
        LIN.error = null;
        LIN.fails = 0;
      } catch (e) {
        if (my === LIN.seq) {
          LIN.error = linFail(e);
          LIN.fails += 1;
          // a second failure at the same position falls back to Live instead of leaving the panel stuck
          if (LIN.fails >= 2 && LIN.step !== null) { LIN.step = null; LIN.fails = 0; LIN.pages = []; LIN.notice = 'That position could not be loaded, so Live is shown.'; again = true; }
        }
      } finally { if (my === LIN.seq) { LIN.busy = false; LIN.at = Date.now(); } }
      if (!alive || my !== LIN.seq) return;
      const next = LIN.pending;
      LIN.pending = null;
      if (next) { LIN.step = next.step; LIN.notice = null; loadLineage(); return; }   // the latest choice made while busy
      drawLineage(true);
      if (again) loadLineage();
      else if (LIN.sel && !WB.busy && !WB.editing && !WB.confirming && Date.now() - WB.at > 7000) loadWorkbench();   // keep the side panel as fresh as the graph
    }
    function drawLineage(force = false) {
      const box = $('hcn-lineage');
      if (!box) return;
      if (!LIN.bound) bindLineage(box);
      // live refresh only at the head of page one: a replay is static and "Load more" pages must survive
      if (!force && LIN.step === null && !LIN.busy && LIN.pages.length <= 1 && Date.now() - LIN.at > 8000) { loadLineage(); }
      else if (!force && !LIN.at && !LIN.busy) { loadLineage(); return; }
      if (!CTX.busy && Date.now() - CTX.at > 15000) { CTX.at = Date.now(); loadContext(); }
      const m = linModel();
      // the layout feeds the SVG's arrow-key navigation only; above the draw cap there is no SVG, and laying out thousands of
      // nodes on every keystroke of the filter box cost 0.5-2.4 s per key (T16 large-graph gate)
      LIN.layout = LIN.mode !== 'table' ? layoutCached(m.filtered) : null;
      refreshSessions();
      const html = renderLineage(m);
      drawConsults();
      if (LIN.html === html && box.innerHTML !== '') return;
      const a = document.activeElement;
      let key = a && box.contains(a) ? focusKey(a) : null;
      // While a request is in flight the controls are drawn disabled and a disabled control cannot keep focus, so remember
      // where focus was when the wait began and give it back when the answer is drawn (keyboard users keep their place).
      const waiting = Boolean(WB.busy || LIN.busy);
      if (waiting && !LIN.wasWaiting) LIN.waitFocus = key;
      if (!waiting && LIN.wasWaiting && !key && (!a || a === document.body) && LIN.waitFocus) key = LIN.waitFocus;
      if (!waiting) LIN.waitFocus = null;
      LIN.wasWaiting = waiting;
      const pos = key && a && box.contains(a) && typeof a.selectionStart === 'number' ? a.selectionStart : null;
      LIN.html = html;
      // Morphed in place: unchanged nodes (the SVG, table rows, replay controls, workbench form) keep
      // their nodes and any focus/scroll; a focused control that left is handed to its equivalent via
      // the focusKey/focusSelector/successor logic below, so keyboard users keep their place.
      const stranded = morph(box, html);
      handOff(stranded, () => null);
      const sel = key && focusSelector(key);
      const el = sel && box.querySelector(sel);
      if (key && !el) {                                                    // the control is gone: its logical successor, else the panel
        const next = focusSuccessor(key).map((q) => box.querySelector(q)).find(Boolean);
        if (next) { if (!next.matches('button,a[href],input,select,textarea,[tabindex]')) next.setAttribute('tabindex', '-1'); next.focus({ preventScroll: true }); } else refocus(box, null);
      }
      if (el) { el.focus({ preventScroll: true }); if (pos !== null && el.setSelectionRange && (el.type === 'search' || el.type === 'textarea' || el.type === 'text')) try { el.setSelectionRange(pos, pos); } catch { /* not selectable */ } }
    }
    // ---- pan / zoom of the graph viewport. The view is plain state; every change re-draws only the nodes now in view.
    const curView = () => LIN.vp || (LIN.layout ? fitView(LIN.layout, LIN.vpW, LIN.vpH) : null);
    const setView = (v) => { if (!LIN.layout || !v) return; LIN.vp = clampView(v, LIN.layout, LIN.vpW, LIN.vpH); drawLineage(true); };
    const revealNode = (id) => {
      const p = LIN.layout && LIN.layout.placed.get(id);
      const v = curView();
      if (p && v && !isVisible(v, p, LIN.vpW, LIN.vpH)) LIN.vp = clampView(centerOn(v, p, LIN.vpW, LIN.vpH), LIN.layout, LIN.vpW, LIN.vpH);
    };
    function vpAct(act) {
      const v = curView();
      // After a focus change the selected node is centred in the new layout, so it is never left off-screen
      const recentre = () => {
        const m = linModel(); const lay = layoutCached(m.filtered); const p = LIN.sel && lay.placed.get(LIN.sel);
        const base = fitView(lay, LIN.vpW, LIN.vpH);
        LIN.vp = p ? clampView(centerOn(base, p, LIN.vpW, LIN.vpH), lay, LIN.vpW, LIN.vpH) : null;
      };
      if (act === 'focus') { if (LIN.sel) { LIN.focusId = LIN.sel; recentre(); drawLineage(true); } return; }
      if (act === 'unfocus') { LIN.focusId = null; recentre(); drawLineage(true); return; }
      if (!v) return;
      if (act === 'fit') { LIN.vp = null; drawLineage(true); }
      else if (act === 'end') { const last = LIN.layout && LIN.layout.columns.at(-1); const p = last && LIN.layout.placed.get(last[0]); if (p) setView(centerOn(v, p, LIN.vpW, LIN.vpH)); }
      else if (act === 'in') setView(zoomAt(v, 1.25, LIN.vpW / 2, LIN.vpH / 2));
      else if (act === 'out') setView(zoomAt(v, 1 / 1.25, LIN.vpW / 2, LIN.vpH / 2));
    }
    function bindViewport(box) {
      // The pointer and the wheel act on the viewport; nodes keep their own click. A drag that starts on a node is a click.
      let drag = null;
      let lastDraw = 0;
      const scaleOf = (svg) => { const r = svg.getBoundingClientRect(); return r.width && LIN.vpW ? r.width / LIN.vpW : 1; };
      box.addEventListener('pointerdown', (e) => {
        const vpEl = e.target.closest && e.target.closest('[data-viewport]');
        if (!vpEl || e.button !== 0) return;
        const svg = vpEl.querySelector('svg.lin-svg'); const v = curView();
        if (!svg || !v) return;
        drag = { x: e.clientX, y: e.clientY, v, s: scaleOf(svg), svg, id: e.pointerId, moved: false, el: vpEl };
      });
      box.addEventListener('pointermove', (e) => {
        if (!drag || e.pointerId !== drag.id) return;
        if (e.buttons === 0) { drag = null; return; }       // released outside the page: never keep panning
        const dx = (e.clientX - drag.x) / drag.s; const dy = (e.clientY - drag.y) / drag.s;
        if (!drag.moved && Math.hypot(dx, dy) < 3) return;
        if (!drag.moved) {
          // Capture only once it is a drag: capturing at pointerdown would send a plain click on a node to the viewport instead of the node.
          try { drag.el.setPointerCapture(e.pointerId); } catch { /* capture is a nicety */ }
          drag.el.classList.add('lin-viewport--drag');
        }
        drag.moved = true;
        LIN.vp = clampView(panBy(drag.v, -dx, -dy), LIN.layout, LIN.vpW, LIN.vpH);
        drag.svg.setAttribute('viewBox', `${LIN.vp.x} ${LIN.vp.y} ${LIN.vpW / LIN.vp.k} ${LIN.vpH / LIN.vp.k}`);
        if (Date.now() - lastDraw > 120) { lastDraw = Date.now(); drawLineage(true); drag.svg = box.querySelector('[data-viewport] svg.lin-svg') || drag.svg; }
      });
      const end = (e) => {
        if (!drag || e.pointerId !== drag.id) return;
        const was = drag; drag = null;
        const vpEl = box.querySelector('[data-viewport]'); if (vpEl) vpEl.classList.remove('lin-viewport--drag');
        if (was.moved) drawLineage(true);
      };
      box.addEventListener('pointerup', end); box.addEventListener('pointercancel', end);
      // Plain wheel scrolls the page; the wheel with Ctrl, Cmd or Alt zooms around the pointer.
      box.addEventListener('wheel', (e) => {
        const vpEl = e.target.closest && e.target.closest('[data-viewport]');
        if (!vpEl || !(e.ctrlKey || e.metaKey || e.altKey)) return;
        const svg = vpEl.querySelector('svg.lin-svg'); const v = curView();
        if (!svg || !v) return;
        e.preventDefault();
        const r = svg.getBoundingClientRect(); const s = scaleOf(svg);
        setView(zoomAt(v, e.deltaY < 0 ? 1.15 : 1 / 1.15, (e.clientX - r.left) / s, (e.clientY - r.top) / s));
      }, { passive: false });
    }
    function bindLineage(box) {
      LIN.bound = true;
      if (typeof ResizeObserver === 'function') {            // the viewport follows the panel: a rotated phone or a resized window redraws at its new width
        let lastW = box.clientWidth; let timer = 0;
        new ResizeObserver(() => { if (Math.abs(box.clientWidth - lastW) < 8 || !box.clientWidth) return; lastW = box.clientWidth; clearTimeout(timer); timer = setTimeout(() => { if (alive) drawLineage(true); }, 120); }).observe(box);
      }
      bindViewport(box);
      let slide = 0;
      // A node that takes focus (Tab into the single tab stop, a click on a list) is brought into view first: focus is never off screen.
      box.addEventListener('focusin', (e) => {
        const n = e.target.closest && e.target.closest('g[data-node]');
        const p = n && LIN.layout && LIN.layout.placed.get(n.dataset.node);
        const v = p && curView();
        if (p && v && !isVisible(v, p, LIN.vpW, LIN.vpH)) {
          // move the window now (the focused node is already in the DOM, so nothing is replaced under focus), re-cull a moment later
          revealNode(n.dataset.node);
          const svg = n.closest('svg'); if (svg && LIN.vp) svg.setAttribute('viewBox', `${LIN.vp.x} ${LIN.vp.y} ${LIN.vpW / LIN.vp.k} ${LIN.vpH / LIN.vp.k}`);
          clearTimeout(LIN.revealTimer); LIN.revealTimer = setTimeout(() => { if (alive) drawLineage(true); }, 200);
        }
      });
      const goStep = (t) => {
        const step = t === 'live' ? null : t;
        if (LIN.busy) { LIN.pending = { step }; drawLineage(true); return; }      // keep the LATEST choice, apply it next
        LIN.step = step; LIN.notice = null; LIN.error = null; if (step === null) LIN.pages = []; loadLineage();
      };
      box.addEventListener('click', (e) => {
        const open = e.target.closest('[data-ctx-open]');
        if (open) { pickNode(open.dataset.ctxOpen); drawLineage(true); return; }
        const cx = e.target.closest('[data-ctx-act]');
        if (cx) { ctxAct(cx.dataset.ctxAct, cx.dataset.ctxId); return; }
        const wb = e.target.closest('[data-wb-act]');
        if (wb && wb.dataset.wbAct === 'edge') {
          WB.edge = { edge_type: wb.dataset.wbEdgeType, from_id: wb.dataset.wbFrom, to_id: wb.dataset.wbTo };
          WB.editing = null; WB.history = null; loadWorkbench();
          return;
        }
        if (wb) { wbAct(wb.dataset.wbAct, wb.dataset.wbId); return; }
        const ta = e.target.closest('[data-term-auth]');
        if (ta) { termAuth(ta.dataset.termAuth); return; }
        const mm = e.target.closest('svg.lin-mm');
        if (mm && LIN.layout) {
          const r = mm.getBoundingClientRect(); const v = curView();
          if (r.width && r.height && v) { const t = minimapTarget(LIN.layout, (e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height); setView({ ...v, x: t.x - LIN.vpW / v.k / 2, y: t.y - LIN.vpH / v.k / 2 }); }
          return;
        }
        const vpb = e.target.closest('[data-vp]');
        if (vpb) { vpAct(vpb.dataset.vp); return; }
        const n = e.target.closest('[data-node]');
        if (n) { pickNode(n.dataset.node); if (!n.closest('svg')) revealNode(n.dataset.node); drawLineage(true); return; }
        const r = e.target.closest('[data-replay]');
        if (e.target.closest('[data-retry]')) { loadLineage(); return; }
        if (r && r.dataset.replay !== 'slider') { goStep(replayTarget(LIN.asOf, r.dataset.replay)); return; }
        const v = e.target.closest('[data-view]');
        if (v) { LIN.view = v.dataset.view; savePrefs(); if (LIN.view === 'gantt' && !LIN.timeline) loadLineage(); else drawLineage(true); return; }
        const md = e.target.closest('[data-mode]');
        if (md) { LIN.mode = md.dataset.mode; savePrefs(); drawLineage(true); return; }
        if (e.target.closest('[data-more]') && !LIN.busy) loadLineage(true);
      });
      // `toggle` does not bubble: capture it, so the person's choice survives every redraw
      box.addEventListener('toggle', (e) => { if (e.target.matches && e.target.matches('details[data-filterbox]')) LIN.filtersOpen = e.target.open; }, true);
      box.addEventListener('submit', (e) => {
        e.preventDefault();                                              // Enter in the search box filters; it never navigates
        const cf = e.target.closest && e.target.closest('[data-ctx-form]');
        if (cf && !CTX.busy) { ctxSubmit(cf.dataset.ctxForm, cf.dataset.ctxId, new FormData(cf)); return; }
        const f = e.target.closest && e.target.closest('[data-wb-form]');
        if (f && !WB.busy) wbSubmit(f.dataset.wbForm, f.dataset.wbId, new FormData(f));
      });
      box.addEventListener('input', (e) => {
        const cfld = e.target.dataset.ctxField;
        if (cfld) { CTX.askDraft[cfld] = e.target.value; return; }
        const wf = e.target.dataset.wbField;
        if (wf) {                                                       // drafts live in memory so a refresh never eats typing
          if (wf === 'edit' && WB.editing) WB.editing.text = e.target.value;
          else if (wf in WB.draft) WB.draft[wf] = e.target.value;
          return;
        }
        const f = e.target.dataset.filter;
        if (f === 'q') {
          LIN.filters.q = e.target.value; LIN.vp = null;      // a different set of nodes: start from the fitted view
          // Above the draw cap one redraw rebuilds thousands of table rows: coalesce a burst of keystrokes into one redraw so
          // typing itself stays responsive. Small graphs redraw at once, as before.
          const loaded = LIN.pages.reduce((n, p) => n + ((p && p.nodes) || []).length, 0);
          if (loaded > 300) { clearTimeout(LIN.qTimer); LIN.qTimer = setTimeout(() => { if (alive) drawLineage(true); }, 250); } else drawLineage(true);
        }
        else if (e.target.dataset.replay === 'slider') {
          const out = box.querySelector('.lin-replay__status');
          if (out) out.textContent = `Position ${e.target.value} of ${e.target.max}. Release to load.`;
        }
      });
      box.addEventListener('change', (e) => {
        const t = e.target;
        if (t.dataset.wbField === 'action') { WB.draft.action = t.value; drawLineage(true); return; }
        if (t.dataset.replay === 'slider') { clearTimeout(slide); const v = t.value; slide = setTimeout(() => goStep(replayTarget(LIN.asOf, 'slider', v)), 150); return; }
        if (t.dataset.clock !== undefined) { LIN.clock = t.value; savePrefs(); drawLineage(true); return; }
        const f = t.dataset.filter;
        if (f === 'type' || f === 'etype') {
          const set = f === 'type' ? LIN.filters.types : LIN.filters.edgeTypes;
          if (t.checked) set.add(t.value); else set.delete(t.value);
          LIN.vp = null; drawLineage(true);
        } else if (f === 'state') { LIN.filters.state = t.value; LIN.vp = null; drawLineage(true); }
      });
      box.addEventListener('keydown', (e) => {
        const n = e.target.closest && e.target.closest('g[data-node]');
        const inVp = e.target.closest && e.target.closest('[data-viewport]');
        if (inVp && !e.ctrlKey && !e.metaKey && !e.altKey) {
          const v = curView();
          const step = 80;
          const pan = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
          if (v && (e.key === '+' || e.key === '=')) { e.preventDefault(); setView(zoomAt(v, 1.25, LIN.vpW / 2, LIN.vpH / 2)); return; }
          if (v && (e.key === '-' || e.key === '_')) { e.preventDefault(); setView(zoomAt(v, 1 / 1.25, LIN.vpW / 2, LIN.vpH / 2)); return; }
          if (e.key === '0') { e.preventDefault(); LIN.vp = null; drawLineage(true); return; }
          if (v && pan && (e.shiftKey || !n)) { e.preventDefault(); setView(panBy(v, pan[0], pan[1])); return; }
        }
        if (n) {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickNode(n.dataset.node); drawLineage(true); return; }
          if (e.key === 'Escape') { pickNode(null); drawLineage(true); return; }
          const to = LIN.layout && nextNode(LIN.layout, n.dataset.node, e.key);
          if (to) { e.preventDefault(); LIN.focus = to; revealNode(to); drawLineage(true); box.querySelector(`g[data-node="${CSS.escape(to)}"]`)?.focus(); }
          return;
        }
        const tab = e.target.closest && e.target.closest('[data-view]');
        if (tab && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) {
          e.preventDefault(); LIN.view = tab.dataset.view === 'graph' ? 'gantt' : 'graph';
          if (LIN.view === 'gantt' && !LIN.timeline) loadLineage(); else drawLineage(true);
          box.querySelector(`[data-view="${LIN.view}"]`)?.focus();
        }
      });
    }
    // ---- T15: attention inbox, evidence-based progress, analytics (server-derived; the page only shows them)
    const ATT = { data: null, error: null, notice: null, showQuiet: false, at: 0, prog: null, progError: null, stats: null, statsError: null, pat: 0 };
    const attErr = (e) => (e && e.status === 403 ? 'The attention inbox, progress and analytics are not enabled for this room.' : 'Could not load this view. It will retry.');
    async function loadAttention(force) {
      if (!force && Date.now() - ATT.at < 15000) { drawAttention(); return; }
      ATT.at = Date.now();
      try { ATT.data = await api.attention(roomId, ATT.showQuiet); ATT.error = null; setBadge('attention', (ATT.data.items || []).length); }
      catch (e) { ATT.error = attErr(e); }
      drawAttention();
    }
    function drawAttention() {
      const h = $('hcn-attention');
      if (!h) return;
      if (!h.dataset.bound) {
        h.dataset.bound = '1';
        const act = async (fn) => { try { await fn(); ATT.notice = null; } catch (e) { ATT.notice = e && e.status === 429 ? 'Too many changes; try again in a minute.' : 'That did not work.'; } await loadAttention(true); };
        h.addEventListener('click', (e) => {
          const b = e.target.closest('[data-attn-act]');
          if (!b) return;
          const a = b.dataset.attnAct;
          if (a === 'toggle-quiet') { ATT.showQuiet = !ATT.showQuiet; loadAttention(true); return; }
          if (a === 'ack') act(() => api.attentionAck(roomId, ackBody(b.dataset.attnKey, b.dataset.attnFp)));
          else if (a === 'restore') act(() => api.attentionRestore(roomId, restoreBody(b.dataset.attnKey)));
        });
        h.addEventListener('change', (e) => {
          const sel = e.target.closest('[data-attn-snooze]');
          if (sel && sel.value) act(() => api.attentionSnooze(roomId, snoozeBody(sel.dataset.attnKey, sel.dataset.attnFp, sel.value)));
        });
      }
      put('hcn-attention', renderAttention({ data: ATT.data, error: ATT.error, notice: ATT.notice, showQuiet: ATT.showQuiet }));
    }
    async function loadProgress(force) {
      if (!force && Date.now() - ATT.pat < 30000) return;
      ATT.pat = Date.now();
      const [p, a] = await Promise.allSettled([api.progress(roomId), api.analytics(roomId)]);
      ATT.prog = p.status === 'fulfilled' ? p.value : ATT.prog; ATT.progError = p.status === 'rejected' ? attErr(p.reason) : null;
      ATT.stats = a.status === 'fulfilled' ? a.value : ATT.stats; ATT.statsError = a.status === 'rejected' ? attErr(a.reason) : null;
      put('hcn-progress', renderProgress({ data: ATT.prog, error: ATT.progError }));
      put('hcn-analytics', renderAnalytics({ data: ATT.stats, error: ATT.statsError }));
    }
    // the count is painted (aria-hidden) and spoken as "Tasks, 3 waiting": the button's own name never glues a number onto the word ("Tasks3")
    const setBadge = (k, n) => {
      const b = $(`hcn-b-${k}`); if (!b) return;
      b.hidden = !n; patchText(b, n > 99 ? '99+' : String(n));
      const tab = b.closest('[role="tab"]'); if (tab) tab.setAttribute('aria-label', n ? `${tab.dataset.label}, ${n > 99 ? 'more than 99' : n} waiting` : tab.dataset.label);
    };
    const loadMine = async () => { try { S.mine = (await api.agents()).agents.filter((x) => !x.external); }   // a remote agent is bound to its invite: nothing to grant
       catch { /* keep the old list */ } };
    function paint() {
      setBadge('agents', (S.requests || []).length);
      setBadge('tasks', S.tasks.filter((t) => ['pending', 'accepted'].includes(t.status)).length);
      if (sub === 'tasks') drawTasks();
      else if (sub === 'flow') drawFlow();
      else if (sub === 'links') put('hcn-links', renderLinks(S.links, S.missions));
      else if (sub === 'tabs') drawTabs();
      else if (sub === 'proposals') drawProposals();
      else if (sub === 'lineage') drawLineage();
      else if (sub === 'attention') loadAttention(false);
      else if (sub === 'progress') loadProgress(false);
      else if (sub === 'agents') { drawRoomAgents(); drawMyAgents(); drawRequests(); drawHelpers(); }
    }
    const paintPanel = async () => {
      if (sub === 'agents' && canPost) await loadMine();
      if (sub === 'agents') await loadHelpers();
      if (sub === 'people') await drawPeople();
      paint();
    };
    const showSub = (k) => {
      sub = k; subTab.set(roomId, k);
      root.querySelectorAll('[data-sub]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.sub === k)));
      root.querySelectorAll('.cx-panel').forEach((p) => { p.hidden = p.id !== `hcn-p-${k}`; });
      if (k === 'chat') { log.scrollTop = log.scrollHeight; composer?.focus(); }
      paintPanel();
    };
    // each sub-tab controls one panel, and the panel says which tab labels it (WAI-ARIA tabs pattern)
    root.querySelectorAll('[role="tab"][data-sub]').forEach((b) => { const p = $(`hcn-p-${b.dataset.sub}`); if (p) { p.setAttribute('role', 'tabpanel'); p.setAttribute('aria-labelledby', b.id); p.removeAttribute('aria-label'); } });
    root.querySelectorAll('[data-sub]').forEach((b) => { b.onclick = () => showSub(b.dataset.sub); });
    root.querySelectorAll('.cx-panel').forEach((p) => { p.hidden = p.id !== `hcn-p-${sub}`; });

    function mentionCtx() {
      return { people: S.people, commands: ROOM_COMMANDS,
        agents: S.agents.map((a) => ({ ...a, label: namedAgent(a).display_name || '' })) };
    }
    // ---- one long-poll feeds every panel (replaces five timers); it sleeps while the tab is hidden
    const apply = (s) => {
      if (s.agents) { S.agents = s.agents; S.agents.forEach((a) => agentName.set(a.agent_id, a.name)); }
      if (s.people) S.people = s.people;
      S.links = s.links ?? null; S.missions = s.missions ?? null;
      S.tasks = s.tasks || []; S.flow = s.flow || null; S.requests = s.requests || null;
      S.agents.forEach(a=>agentName.set(a.agent_id,namedAgent(a).display_name || a.name));
      addEvents(s.events || []);
      cursor = s.next_cursor ?? cursor;
      paintPanel();
    };
    const sleep = (ms) => new Promise((r) => { setTimeout(r, ms); });
    (async () => {
      while (alive) {
        if (document.hidden) { await new Promise((r) => { wake = r; }); continue; }
        ctl = new AbortController();
        try {
          const s = await api.state(roomId, since, cursor, 8, ctl.signal);
          if (!alive) return;
          backoff = 1000;
          onFreshness(true);
          if (s.changed) { apply(s); since = s.has_more ? -1 : s.seq; } else since = s.seq;
        } catch (e) {
          if (!alive) return;
          if (e.name === 'AbortError') continue;
          if (e instanceof AuthError) { dispose(); store.clear(); start(); return; }
          if (/not found/.test(e.message)) { dispose(); go('/rooms'); return; }
          onFreshness(false, `Room: ${e.message || 'request failed'}`);
          await sleep(backoff); backoff = Math.min(backoff * 2, 15000);
        }
      }
    })();

    // ---- forms
    const taskForm = $('hcn-task');
    if (taskForm) {
      taskForm.onsubmit = async (ev) => {
        ev.preventDefault();
        const f = new FormData(taskForm);
        try {
          await api.createTask(roomId, f.get('target'), f.get('text'));
          taskForm.querySelector('textarea').value = ''; $('hcn-task-msg').textContent = ''; kick();
        } catch (e) { $('hcn-task-msg').textContent = e.message; }
      };
    }
    log.addEventListener('click', (ev) => {                       // a reference chip opens the item in the Lineage tab
      const open = ev.target.closest && ev.target.closest('[data-ctx-open]');
      if (open && !guest) { showSub('lineage'); pickNode(open.dataset.ctxOpen); drawLineage(true); }
    });
    if ($('hcn-composer')) {
      composer = mountComposer($('hcn-composer'), {
        draftKey: `collab:${me.user_id}:${roomId}`,
        placeholder: 'Message the room… @ people, agents and files · / commands',
        hint: 'Enter sends · Shift+Enter adds a line · @gert.pi … gives that agent a task',
        providers, context: mentionCtx,
        check: (text) => {
          const ctx = mentionCtx(), notes = [];
          for (const a of ambiguousMentions(text, ctx)) notes.push(`@${a.name} is ambiguous — use ${a.forms.length ? a.forms.join(' or ') : '@owner.harness'}.`);
          const missing = unresolvedMentions(text, ctx);
          if (missing.length) notes.push(`${missing.map((n) => `@${n}`).join(', ')} addresses nobody here; it will be sent as plain text.`);
          return notes.join(' ');
        },
        onSend: async (text) => {
          // The text goes out exactly as typed: the server derives mentions, and a harness reads what was written.
          const lead = /^@([a-z0-9._-]{1,48})\s+\S/i.exec(text.trim());
          // the same precedence as the server: a group or a person handle is never an agent's task
          const found = lead && resolveToken(lead[1].toLowerCase(), mentionCtx());
          if (found?.ambiguous) throw new Error(`More than one agent or person is called @${lead[1]}; use ${found.ambiguous.length ? found.ambiguous.join(' or ') : '@owner.harness'}`);
          const hit = found?.hit;
          const target = hit && hit.kind === 'agent' && S.agents.find((a) => a.agent_id === hit.id);
          const mention = target && target.actions.includes('execute');
          if (mention) { // the room's authenticated leading-agent task contract, now with the verbatim text
            await api.createTask(roomId, target.agent_id, text);
          } else { await api.say(roomId, text, CTX.attach ? contextBody(CTX.attach) : undefined); CTX.attach = null; drawAttach(); }
          if (alive) { kick(); log.scrollTop = log.scrollHeight; }
          return { status: 'delivered', message: mention ? 'Task created' : 'Message posted' };
        },
      });
    }
    const inviteForm = $('hcn-invite');
    if (inviteForm) {
      inviteForm.onsubmit = async (ev) => {
        ev.preventDefault();
        const f = new FormData(ev.target);
        const msg = $('hcn-invite-msg');
        try {
          await api.invite(roomId, f.get('email'), f.get('role'));
          msg.textContent = 'Invited. They get access when they sign in here.';
          ev.target.reset(); $('hcn-people').dataset.h = ''; paintPanel();
        } catch (e) { msg.textContent = e.message; }
      };
    }
    const extForm = $('hcn-ext-form');
    if (extForm) {
      extForm.onsubmit = async (ev) => {
        ev.preventDefault();
        const f = new FormData(ev.target);
        const msg = $('hcn-ext-msg');
        const box = $('hcn-ext-code');
        try {
          const out = await api.inviteExternal(roomId, {
            label: String(f.get('label') || ''), actions: f.get('post') ? ['read', 'post'] : ['read'],
            lineage: !!f.get('lineage'), proposals: !!f.get('proposals'), ttl_seconds: Number(f.get('ttl')), credential_ttl_seconds: Number(f.get('cred')) });
          msg.textContent = '';
          // shown once: the server keeps only a digest of the code
          box.innerHTML = `<div class="cx-msg">Give this one-time code to your coworker. It is shown only now, works once and expires ${esc(new Date(out.expires_at * 1000).toISOString())}.</div>`
            + `<code class="cx-code" id="hcn-ext-codeval" tabindex="0">${esc(out.code)}</code>`
            + `<div class="cx-msg">Their bridge redeems it with <code>POST /api/v1/collab/agents/join</code> (<code>{"token": "…"}</code>) and then reports with the credential it receives. See docs/operations/external-agents.md.</div>`;
          ev.target.reset(); S.tabsAt = 0; $('hcn-ext-invites').dataset.h = ''; drawTabs();
        } catch (e) { msg.textContent = e.message; box.textContent = ''; }
      };
    }
    const delegForm = $('hcn-deleg');
    if (delegForm) {
      delegForm.onsubmit = async (ev) => {
        ev.preventDefault();
        const f = new FormData(ev.target);
        const msg = $('hcn-deleg-msg');
        try {
          await api.delegate(roomId, delegationBody({
            target: f.get('target'), text: f.get('text'), paths: f.get('paths'), nodes: f.get('nodes'), questions: f.get('questions'),
            kinds: f.getAll('kind'), maxProposals: f.get('maxProposals'), maxSteps: f.get('maxSteps'), maxBytes: f.get('maxBytes'), expires: f.get('expires') }));
          msg.textContent = 'Delegated.';
          ev.target.reset(); $('hcn-delegs').dataset.h = ''; drawProposals();
        } catch (e) { msg.textContent = e.message; }
      };
    }
    log.scrollTop = log.scrollHeight;
  }

  if (standalone) window.addEventListener('hashchange', onHash);
  start();
  return { dispose: () => { dispose(); window.removeEventListener('hashchange', onHash); } };
}

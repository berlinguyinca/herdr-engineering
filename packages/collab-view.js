// Collaboration rooms: sign in with the BinView user pool, create rooms, chat, and invite
// people by email. All remote text is untrusted: it is escaped, never interpreted.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import {
  AuthError, CollabApi, TokenStore, authorizeUrl, exchangeCode, parseRedirect, pkcePair, randomString,
} from './herdr-web-client/collab.js';

const store = new TokenStore(window.sessionStorage);
const api = new CollabApi(() => store.get());
const ROLES = [['contributor', 'Can chat'], ['viewer', 'Read only'], ['guest', 'Guest (read only, expires)']];
const ROLE_LABEL = { owner: 'Owner', contributor: 'Can chat', viewer: 'Read only', guest: 'Guest' };

async function loadConfig() {
  const res = await fetch('/api/v1/collab/config');
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

const pill = (text, color = 'var(--text-muted)') =>
  `<span class="hd-status" style="--sc:${color}"><span class="hd-status__dot" aria-hidden="true">●</span><span class="hd-status__label">${esc(text)}</span></span>`;
const roleLabel = (r) => ROLE_LABEL[r] || r;
const shortId = (id) => `…${String(id || '').slice(-6)}`;

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

export function mountCollab(root) {
  let timer = null;
  let cursor = 0;
  let lastCfg = null;
  let me = null;
  const standalone = root.dataset.standalone === '1';
  const dispose = () => { clearInterval(timer); timer = null; };
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

  function frame(active, content) {
    dispose();
    if (!standalone) { root.innerHTML = content; return; }
    const link = (path, label) => `<a class="hd-shell__link${active === path ? ' active' : ''}" href="#${path}">${label}</a>`;
    root.innerHTML = `<div class="hd-shell">
      <aside class="hd-shell__sidebar">
        <div class="hd-shell__brand"><span class="cx-logo" aria-hidden="true">◆</span>HerdR Collaboration</div>
        <nav class="hd-shell__nav" aria-label="Collaboration">${link('/rooms', 'Rooms')}${link('/agents', 'AI agents')}</nav>
        <div class="cx-user"><span>${esc(me ? (me.email || me.user_id) : '')}</span><button id="hcn-out" class="hd-btn hd-btn--ghost hd-btn--sm" type="button">Sign out</button></div>
      </aside>
      <main class="hd-shell__main"><div class="hd-shell__content">${content}</div></main></div>`;
    $('hcn-out').onclick = () => { store.clear(); window.location.hash = ''; start(); };
  }

  function centered(inner) {
    dispose();
    root.innerHTML = `<div class="cx-center"><div class="hd-card cx-signin"><div class="cx-logo" aria-hidden="true">◆</div>${inner}</div></div>`;
  }

  async function start() {
    const cfg = await loadConfig();
    lastCfg = cfg;
    if (!cfg) {
      centered('<h1>Collaboration</h1><p role="status">The collaboration network is not enabled on this server.</p>');
      return;
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
        <p>Shared rooms for people and their AI agents. Sign in with your BinView account; you need to be in the <strong>local.dev</strong> group.</p>
        ${err ? `<p class="cx-msg" role="alert">${esc(err)}</p>` : ''}
        <button id="hcn-signin" class="hd-btn" type="button">Sign in</button>`);
      $('hcn-signin').onclick = () => signIn(cfg);
      return;
    }
    try {
      me = await api.link(store.get().id);   // records your verified email so others can invite you
      await route();
    } catch (e) {
      if (e instanceof AuthError) { store.clear(); return start(); }
      centered(`<h1>Collaboration</h1><p class="cx-msg" role="alert">${esc(e.message)}</p>
        <p>If you were just added to the group, sign out and sign in again.</p>
        <button id="hcn-out2" class="hd-btn hd-btn--ghost" type="button">Sign out</button>`);
      $('hcn-out2').onclick = () => { store.clear(); start(); };
    }
  }

  async function route() {
    if (!me) return start();
    const hash = window.location.hash.replace(/^#/, '');
    const room = /^\/room\/(.+)$/.exec(hash);
    if (room) return showRoom(decodeURIComponent(room[1]));
    if (hash === '/agents') return showAgents();
    return showRooms();
  }

  async function showRooms() {
    const { rooms } = await api.rooms();
    frame('/rooms', `<div class="cx-head"><div><h1>Rooms</h1><p class="cx-sub">Conversations you and your agents can take part in.</p></div></div>
      ${rooms.length ? `<div class="hd-grid" id="hcn-rooms">${rooms.map((r) => `<button type="button" class="hd-card cx-room" data-room="${esc(r.room_id)}">
          <span class="hd-card__title">${esc(r.name)}</span>
          <span class="cx-row cx-row--between">${pill(roleLabel(r.role), r.role === 'owner' ? 'var(--accent)' : 'var(--status-active)')}<span class="hd-card__sub">Open →</span></span></button>`).join('')}</div>`
        : '<div class="hd-empty">No rooms yet. Create your first one below.</div>'}
      <section class="cx-section"><h2>New room</h2>
        <form id="hcn-new" class="hd-card cx-form"><label class="cx-field">Room name<input name="name" required maxlength="120" placeholder="e.g. Metabolomics review"></label>
          <button class="hd-btn" type="submit">Create room</button><span id="hcn-new-msg" class="cx-msg" role="status"></span></form></section>`);
    document.querySelectorAll('[data-room]').forEach((b) => { b.onclick = () => { window.location.hash = `/room/${encodeURIComponent(b.dataset.room)}`; }; });
    $('hcn-new').onsubmit = async (ev) => {
      ev.preventDefault();
      const name = new FormData(ev.target).get('name');
      try {
        const p = await api.createProject(name);
        const r = await api.createRoom(p.project_id, name);
        window.location.hash = `/room/${encodeURIComponent(r.room_id)}`;
      } catch (e) { $('hcn-new-msg').textContent = e.message; $('hcn-new-msg').setAttribute('role', 'alert'); }
    };
  }

  async function showAgents() {
    frame('/agents', `<div class="cx-head"><div><h1>AI agents</h1><p class="cx-sub">Connect Claude Code, Codex, Pi or any MCP client so it can work in your rooms.</p></div>
        <button id="hcn-connect" class="hd-btn" type="button">Connect an AI agent</button></div>
      <div id="hcn-connect-out"></div>
      <section class="cx-section"><h2>Your agents</h2><div id="hcn-agents"></div></section>
      <section class="cx-section">${GUIDE}</section>`);
    await renderAgents();
    $('hcn-connect').onclick = async () => {
      const btn = $('hcn-connect');
      btn.disabled = true;
      try { showCredential(await api.connectAgent()); await renderAgents(); } catch (e) {
        $('hcn-connect-out').innerHTML = `<div class="hd-error" role="alert">${esc(e.message)}</div>`;
      } finally { btn.disabled = false; }
    };
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
    box.innerHTML = live.length ? `<div class="hd-grid">${live.map((a) => `<div class="hd-card cx-stack">
        <div class="cx-row cx-row--between"><span class="hd-card__title">${esc(a.harness_type)}</span>${pill('Active', 'var(--status-ok)')}</div>
        <span class="hd-ident">${esc(a.agent_id)}</span>
        <span class="hd-card__sub">Expires ${esc(new Date(a.expires_at * 1000).toLocaleDateString())} · ${a.rooms.length ? `in ${esc(a.rooms.map((r) => r.name).join(', '))}` : 'not in any room yet'}</span>
        <div><button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-revoke="${esc(a.agent_id)}">Revoke</button></div></div>`).join('')}</div>`
      : '<div class="hd-empty">No connected agents yet.</div>';
    box.querySelectorAll('[data-revoke]').forEach((b) => { b.onclick = async () => { await api.revokeAgent(b.dataset.revoke); renderAgents(); }; });
  }

  async function showRoom(roomId) {
    cursor = 0;
    let info;
    try { info = await api.room(roomId); } catch (e) { window.location.hash = '/rooms'; return; }
    const canInvite = info.role === 'owner';
    const canPost = ['owner', 'contributor'].includes(info.role);
    const names = new Map((info.members || []).map((m) => [m.user_id, m.email || shortId(m.user_id)]));
    frame('/rooms', `<button id="hcn-back" class="cx-back" type="button">← All rooms</button>
      <div class="cx-head"><div><h1>${esc(info.name)}</h1><p class="cx-sub">You are ${esc(roleLabel(info.role).toLowerCase())} in this room.</p></div>${pill(roleLabel(info.role), 'var(--accent)')}</div>
      <div class="cx-room-layout">
        <section class="hd-card cx-chat" aria-label="Conversation">
          <div id="hcn-log" class="cx-log" role="log" aria-live="polite"><div class="hd-empty" id="hcn-empty">No messages yet.</div></div>
          ${canPost ? '<form id="hcn-say" class="cx-compose"><input class="cx-input" name="text" required maxlength="8000" aria-label="Message" placeholder="Write a message…" autocomplete="off"><button class="hd-btn" type="submit">Send</button></form><div id="hcn-say-msg" class="cx-msg" role="status" style="padding:0 12px 10px"></div>'
            : '<div class="cx-msg" style="padding:12px">Read-only access.</div>'}
        </section>
        <aside class="cx-side">
          ${canPost ? '<section class="hd-card"><div class="hd-card__title">Your agents in this room</div><div id="hcn-grant"></div></section>' : ''}
          ${canInvite ? `<section class="hd-card cx-stack"><div class="hd-card__title">People</div><div id="hcn-people"></div>
            <form id="hcn-invite" class="cx-stack"><label class="cx-field">Invite by email<input name="email" type="email" required placeholder="name@example.org"></label>
              <label class="cx-field">Access<select name="role">${ROLES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}</select></label>
              <button class="hd-btn" type="submit">Send invite</button><span id="hcn-invite-msg" class="cx-msg" role="status"></span></form></section>` : ''}
        </aside>
      </div>`);
    $('hcn-back').onclick = () => { window.location.hash = '/rooms'; };
    const log = $('hcn-log');

    const poll = async () => {
      try {
        const page = await api.events(roomId, cursor);
        if (page.events.length) $('hcn-empty')?.remove();
        const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
        for (const e of page.events) {
          const row = document.createElement('div');
          if (e.event_type === 'message') {
            const agent = e.sender.type === 'agent';
            const mine = !agent && e.sender.id === me.user_id;
            const who = agent ? `Agent of ${names.get(e.sender.owner_user_id) || shortId(e.sender.owner_user_id)}`
              : (mine ? 'You' : (names.get(e.sender.id) || shortId(e.sender.id)));
            row.className = `cx-msg-row${mine ? ' cx-msg-row--mine' : ''}${agent ? ' cx-msg-row--agent' : ''}`;
            row.innerHTML = `<span class="cx-meta">${agent ? '🤖 ' : ''}${esc(who)} · ${esc(new Date(e.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))}</span><div class="cx-bubble">${esc(e.text)}</div>`;
          } else {
            row.className = 'cx-event';
            row.textContent = e.event_type.replaceAll('_', ' ');
          }
          log.appendChild(row);
        }
        if (page.events.length) { cursor = page.next_cursor; if (stick) log.scrollTop = log.scrollHeight; }
      } catch (e) {
        if (e instanceof AuthError) { dispose(); store.clear(); start(); } else if (/not found/.test(e.message)) { dispose(); window.location.hash = '/rooms'; }
      }
    };
    await poll();
    log.scrollTop = log.scrollHeight;
    timer = setInterval(poll, 3000);

    const grantBox = $('hcn-grant');
    if (grantBox) {
      const draw = async () => {
        const { agents } = await api.agents();
        const live = agents.filter((a) => a.status === 'active');
        grantBox.innerHTML = live.length ? `<ul class="cx-list">${live.map((a) => {
          const g = a.rooms.find((r) => r.room_id === roomId);
          return `<li><span class="grow">${esc(a.harness_type)}<br><span class="hd-ident">${esc(shortId(a.agent_id))}</span></span>${g
            ? `${pill(g.actions.includes('post') ? 'Read + post' : 'Read only', 'var(--status-ok)')}<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-g="${esc(a.agent_id)}" data-a="read">Read only</button>`
            : `<span class="cx-row"><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-g="${esc(a.agent_id)}" data-a="read">Add (read)</button><button type="button" class="hd-btn hd-btn--sm" data-g="${esc(a.agent_id)}" data-a="read,post">Add (read + post)</button></span>`}</li>`;
        }).join('')}</ul>` : '<div class="hd-empty">No agents yet. <a href="#/agents">Connect one</a>.</div>';
        grantBox.querySelectorAll('[data-g]').forEach((b) => { b.onclick = async () => { await api.grantAgent(roomId, b.dataset.g, b.dataset.a.split(',')); draw(); }; });
      };
      await draw();
    }
    const sayForm = $('hcn-say');
    if (sayForm) {
      sayForm.onsubmit = async (ev) => {
        ev.preventDefault();
        const text = new FormData(sayForm).get('text');
        try { await api.say(roomId, text); sayForm.reset(); $('hcn-say-msg').textContent = ''; await poll(); log.scrollTop = log.scrollHeight; } catch (e) { $('hcn-say-msg').textContent = e.message; }
      };
    }
    if (canInvite) {
      const people = async () => {
        const [room, inv] = await Promise.all([api.room(roomId), api.invites(roomId)]);
        $('hcn-people').innerHTML = `<ul class="cx-list">${room.members.map((m) => `<li><span class="grow">${esc(m.email || shortId(m.user_id))}</span>${pill(roleLabel(m.role), m.role === 'owner' ? 'var(--accent)' : 'var(--status-active)')}${m.role !== 'owner' ? `<button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-rm="${esc(m.user_id)}">Remove</button>` : ''}</li>`).join('')}
          ${inv.invites.map((i) => `<li><span class="grow">${esc(i.email)}</span>${pill('Invited', 'var(--status-attention)')}<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-cancel="${esc(i.invite_id)}">Cancel</button></li>`).join('')}</ul>`;
        document.querySelectorAll('[data-rm]').forEach((b) => { b.onclick = async () => { await api.removeMember(roomId, b.dataset.rm); people(); }; });
        document.querySelectorAll('[data-cancel]').forEach((b) => { b.onclick = async () => { await api.cancelInvite(roomId, b.dataset.cancel); people(); }; });
      };
      await people();
      $('hcn-invite').onsubmit = async (ev) => {
        ev.preventDefault();
        const f = new FormData(ev.target);
        const msg = $('hcn-invite-msg');
        try {
          await api.invite(roomId, f.get('email'), f.get('role'));
          msg.textContent = 'Invited. They get access when they sign in here.';
          ev.target.reset(); people();
        } catch (e) { msg.textContent = e.message; }
      };
    }
  }

  window.addEventListener('hashchange', () => { if (store.get() && me) route(); });
  start();
  return { dispose };
}

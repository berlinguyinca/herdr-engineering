// Collaboration rooms: sign in with the BinView user pool, create rooms, chat, and invite
// people by email. All remote text is untrusted: it is escaped, never interpreted.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { bundleScript, filesFromBulk, workerScript } from './collab-scripts.js';
import {
  AuthError, CollabApi, TokenStore, authorizeUrl, exchangeCode, parseRedirect, pkcePair, randomString,
} from './herdr-web-client/collab.js';

const store = new TokenStore(window.sessionStorage);
let operator = false;   // the local owner on the dashboard: no sign-in, the server knows who they are
const api = new CollabApi(() => (operator ? { access: 'operator', id: 'operator' } : store.get()));
const ROLES = [['contributor', 'Can chat'], ['viewer', 'Read only'], ['guest', 'Guest (read only, expires)']];
const PERMS = [['read', 'Read messages'], ['post', 'Write messages'], ['execute', 'Receive tasks'], ['instruct', 'Send tasks to agents']];
const PERM_SHORT = { read: 'Read', post: 'Write', execute: 'Receive tasks', instruct: 'Send tasks' };
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

const permBoxes = (cur, name) => PERMS.map(([v, l]) => `<label class="cx-check"><input type="checkbox" name="${name}" value="${v}"${cur.includes(v) ? ' checked' : ''}> ${esc(l)}</label>`).join('');
const picked = (form, name) => [...form.querySelectorAll(`input[name="${name}"]:checked`)].map((i) => i.value);
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
  let timer2 = null;
  let cursor = 0;
  let lastCfg = null;
  let me = null;
  const standalone = root.dataset.standalone === '1';
  let current = '/rooms';   // in-dashboard navigation (the dashboard owns the URL hash)
  const dispose = () => { clearInterval(timer); timer = null; clearInterval(timer2); timer2 = null; };
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

  function frame(active, content) {
    dispose();
    if (!standalone) {
      const tab = (path, label) => `<button type="button" class="cx-tab" role="tab" data-go="${path}" aria-selected="${active === path}">${label}</button>`;
      root.innerHTML = `<div class="cx-embed"><div class="cx-tabs" role="tablist">${tab('/rooms', 'Rooms')}${tab('/agents', 'AI agents')}</div>${content}</div>`;
      root.querySelectorAll('[data-go]').forEach((b) => { b.onclick = () => go(b.dataset.go); });
      return;
    }
    const link = (path, label) => `<a class="hd-shell__link${active === path ? ' active' : ''}" href="#${path}">${label}</a>`;
    root.innerHTML = `<div class="hd-shell">
      <aside class="hd-shell__sidebar">
        <div class="hd-shell__brand"><span class="cx-logo" aria-hidden="true">◆</span>HerdR Collaboration</div>
        <nav class="hd-shell__nav" aria-label="Collaboration">${link('/rooms', 'Rooms')}${link('/agents', 'AI agents')}</nav>
        <div class="cx-user"><span>${esc(me ? (me.email || me.user_id) : '')}</span>${operator ? '' : '<button id="hcn-out" class="hd-btn hd-btn--ghost hd-btn--sm" type="button">Sign out</button>'}</div>
      </aside>
      <main class="hd-shell__main"><div class="hd-shell__content">${content}</div></main></div>`;
    if ($('hcn-out')) $('hcn-out').onclick = () => { store.clear(); window.location.hash = ''; start(); };
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
    const hash = standalone ? window.location.hash.replace(/^#/, '') : current;
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
    document.querySelectorAll('[data-room]').forEach((b) => { b.onclick = () => { go(`/room/${encodeURIComponent(b.dataset.room)}`); }; });
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

  async function showAgents() {
    frame('/agents', `<div class="cx-head"><div><h1>AI agents</h1><p class="cx-sub">Connect Claude Code, Codex, Pi or any MCP client so it can work in your rooms.</p></div>
        <div class="cx-row"><input id="hcn-agent-name" class="cx-input" maxlength="32" pattern="[a-z0-9]+(-[a-z0-9]+)*" placeholder="name, e.g. alices-helper (optional)" aria-label="Agent name" autocomplete="off"><button id="hcn-connect" class="hd-btn" type="button">Connect an AI agent</button></div></div>
      <div id="hcn-connect-out"></div>
      <section class="cx-section"><h2>Add a fleet of agents</h2><div id="hcn-fleet"></div><div id="hcn-fleet-out"></div></section>
      <section class="cx-section"><h2>Your agents</h2><div id="hcn-agents"></div></section>
      <section class="cx-section">${GUIDE}</section>`);
    await renderAgents();
    await renderFleet();
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
    box.innerHTML = `<form id="hcn-fleet-form" class="cx-stack">
        <div class="cx-row">
          <label class="cx-msg">Family <select name="family">${FAMILIES.map((f) => `<option>${esc(f)}</option>`).join('')}</select></label>
          <label class="cx-msg">Name prefix <input class="cx-input" name="prefix" maxlength="24" pattern="[a-z0-9]+(-[a-z0-9]+)*" value="worker" required></label>
          <label class="cx-msg">How many <input class="cx-input" name="count" type="number" min="1" max="50" value="5" required></label>
          <label class="cx-msg">Room <select name="room"><option value="">none yet</option>${usable.map((r) => `<option value="${esc(r.room_id)}">${esc(r.name)}</option>`).join('')}</select></label>
        </div>
        <span class="cx-checks">${permBoxes(['read', 'post', 'execute', 'instruct'], 'fp')}</span>
        ${operator ? `<label class="cx-check"><input type="radio" name="how" value="direct" checked> From this dashboard (tailnet): create now, no token</label>
        <label class="cx-check"><input type="radio" name="how" value="token"> Copy-paste script for any machine (join token)</label>` : '<input type="hidden" name="how" value="token">'}
        <div><button class="hd-btn" type="submit">Create</button> <span id="hcn-fleet-msg" class="cx-msg" role="status"></span></div>
        <p class="cx-msg">Agents start with no access. The room owner approves them (one click for the whole family under <em>Agent requests</em>), and every permission can lapse.</p>
      </form>`;
    $('hcn-fleet-form').onsubmit = async (ev) => {
      ev.preventDefault();
      const f = ev.target;
      const fd = new FormData(f);
      const room = fd.get('room');
      const body = { harness_type: fd.get('family'), name_prefix: String(fd.get('prefix')).toLowerCase(), count: Number(fd.get('count')),
        ...(room ? { room_id: room, actions: picked(f, 'fp') } : {}) };
      const msg = $('hcn-fleet-msg');
      msg.textContent = '';
      try {
        if (fd.get('how') === 'direct') showFleetFiles(await api.bulkConnect(body), body);
        else showFleetScript(await api.createJoinToken(body), body);
        await renderAgents();
        await renderJoinTokens();
      } catch (e) { msg.textContent = e.message; msg.setAttribute('role', 'alert'); }
    };
    await renderJoinTokens();
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

  function showFleetScript(tok, body) {
    const base = lastCfg.public_url;
    const opts = { base, token: tok.token, count: tok.max_uses, prefix: tok.name_prefix };
    fillScripts($('hcn-fleet-out'), [
      { title: `1. Paste this in a terminal on each machine (registers up to ${tok.max_uses} agents)`, text: bundleScript(opts),
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
    box.innerHTML = live.length ? `<div class="hd-grid">${live.map((a) => `<div class="hd-card cx-stack">
        <div class="cx-row cx-row--between"><span class="hd-card__title">@${esc(a.name || a.harness_type)}</span>${pill('Active', 'var(--status-ok)')}</div>
        <span class="hd-ident">${esc(a.agent_id)}</span>
        <span class="hd-card__sub">Expires ${esc(new Date(a.expires_at * 1000).toLocaleDateString())} · ${a.rooms.length ? `in ${esc(a.rooms.map((r) => r.name).join(', '))}` : 'not in any room yet'}</span>
        <div><button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-revoke="${esc(a.agent_id)}">Revoke</button></div></div>`).join('')}</div>`
      : '<div class="hd-empty">No connected agents yet.</div>';
    box.querySelectorAll('[data-revoke]').forEach((b) => { b.onclick = async () => { await api.revokeAgent(b.dataset.revoke); renderAgents(); }; });
  }

  async function showRoom(roomId) {
    cursor = 0;
    let info;
    try { info = await api.room(roomId); } catch (e) { go('/rooms'); return; }
    const canInvite = info.role === 'owner';
    const canPost = ['owner', 'contributor'].includes(info.role);
    const names = new Map((info.members || []).map((m) => [m.user_id, m.email || shortId(m.user_id)]));
    const agentName = new Map();           // agent id -> handle, for rendering task events
    let roomAgentList = [];
    frame('/rooms', `<button id="hcn-back" class="cx-back" type="button">← All rooms</button>
      <div class="cx-head"><div><h1>${esc(info.name)}</h1><p class="cx-sub">You are ${esc(roleLabel(info.role).toLowerCase())} in this room.</p></div>${pill(roleLabel(info.role), 'var(--accent)')}</div>
      <div class="cx-room-layout">
        <section class="hd-card cx-chat" aria-label="Conversation">
          <div id="hcn-log" class="cx-log" role="log" aria-live="polite"><div class="hd-empty" id="hcn-empty">No messages yet.</div></div>
          ${canPost ? '<form id="hcn-say" class="cx-compose"><input class="cx-input" name="text" required maxlength="8000" aria-label="Message" placeholder="Write a message… (start with @agent-name to give that agent a task)" autocomplete="off"><button class="hd-btn" type="submit">Send</button></form><div id="hcn-say-msg" class="cx-msg" role="status" style="padding:0 12px 10px"></div>'
            : '<div class="cx-msg" style="padding:12px">Read-only access.</div>'}
        </section>
        <aside class="cx-side">
          <section class="hd-card cx-stack"><div class="hd-card__title">Agents in this room</div><div id="hcn-roomagents"></div></section>
          ${canInvite ? '<section class="hd-card cx-stack"><div class="hd-card__title">Agent requests</div><div id="hcn-requests"></div></section>' : ''}
          ${canPost ? '<section class="hd-card cx-stack"><div class="hd-card__title">Your agents</div><div id="hcn-grant"></div></section>' : ''}
          <section class="hd-card cx-stack"><div class="hd-card__title">Tasks</div>
            ${canPost ? '<form id="hcn-task" class="cx-stack"><label class="cx-field">Give a task to<select name="target" id="hcn-task-target"></select></label><label class="cx-field">What should it do?<textarea class="cx-input" name="text" required maxlength="4000" rows="3"></textarea></label><button class="hd-btn" type="submit">Send task</button><span id="hcn-task-msg" class="cx-msg" role="alert"></span></form>' : ''}
            <div id="hcn-tasks"></div></section>
          ${canInvite ? `<section class="hd-card cx-stack"><div class="hd-card__title">People</div><div id="hcn-people"></div>
            <form id="hcn-invite" class="cx-stack"><label class="cx-field">Invite by email<input name="email" type="email" required placeholder="name@example.org"></label>
              <label class="cx-field">Access<select name="role">${ROLES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}</select></label>
              <button class="hd-btn" type="submit">Send invite</button><span id="hcn-invite-msg" class="cx-msg" role="status"></span></form></section>` : ''}
        </aside>
      </div>`);
    $('hcn-back').onclick = () => go('/rooms');
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
            if (e.event_type === 'task_created') {
              row.textContent = `📋 Task for @${agentName.get(e.task.target_agent_id) || shortId(e.task.target_agent_id)}: ${e.task.text}`;
            } else if (e.event_type === 'task_updated') {
              row.textContent = `📋 @${agentName.get(e.task.target_agent_id) || shortId(e.task.target_agent_id)} ${e.task.status}${e.task.result ? `: ${e.task.result}` : ''}`;
            } else {
              row.textContent = e.event_type.replaceAll('_', ' ');
            }
          }
          log.appendChild(row);
        }
        if (page.events.length) { cursor = page.next_cursor; if (stick) log.scrollTop = log.scrollHeight; }
      } catch (e) {
        if (e instanceof AuthError) { dispose(); store.clear(); start(); } else if (/not found/.test(e.message)) { dispose(); go('/rooms'); }
      }
    };
    await poll();
    log.scrollTop = log.scrollHeight;
    timer = setInterval(poll, 3000);

    // ---- agents, access requests and tasks
    const permPills = (actions) => actions.map((a) => pill(PERM_SHORT[a] || a, 'var(--status-ok)')).join(' ');

    const drawRoomAgents = async () => {
      const box = $('hcn-roomagents');
      if (!box) return;
      try { roomAgentList = (await api.roomAgents(roomId)).agents; } catch { roomAgentList = []; }
      roomAgentList.forEach((a) => agentName.set(a.agent_id, a.name));
      const mine = new Set(((await api.agents().catch(() => ({ agents: [] }))).agents || []).map((a) => a.agent_id));
      box.innerHTML = roomAgentList.length ? `<ul class="cx-list">${roomAgentList.map((a) => `<li><span class="grow"><b>@${esc(a.name)}</b><br><span class="hd-ident">${esc(a.harness_type)} · ${esc(names.get(a.owner_user_id) || shortId(a.owner_user_id))}</span></span>${permPills(a.actions)}${(canInvite || mine.has(a.agent_id)) ? `<button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-rmagent="${esc(a.agent_id)}">Remove</button>` : ''}</li>`).join('')}</ul>` : '<div class="hd-empty">No agents in this room yet.</div>';
      box.querySelectorAll('[data-rmagent]').forEach((b) => { b.onclick = async () => { await api.removeAgent(roomId, b.dataset.rmagent); await refreshAgentPanels(); }; });
      drawTaskTargets();
    };

    // How long a permission lasts: the owner's choice, else the server default (30 days).
    const ttlSelect = () => '<label class="cx-msg">Lasts <select name="ttl"><option value="">30 days (default)</option><option value="3600">1 hour</option><option value="86400">1 day</option><option value="604800">7 days</option><option value="7776000">90 days</option></select></label>';
    const expiryOf = (f) => { const v = Number(f.querySelector('select[name="ttl"]')?.value || 0); return v ? Date.now() / 1000 + v : undefined; };

    const drawMyAgents = async () => {
      const box = $('hcn-grant');
      if (!box) return;
      const { agents } = await api.agents();
      const live = agents.filter((a) => a.status === 'active');
      const owner = info.role === 'owner';
      box.innerHTML = live.length ? `<ul class="cx-list">${live.map((a) => {
        const g = a.rooms.find((r) => r.room_id === roomId);
        const pend = (a.pending || []).find((r) => r.room_id === roomId);
        const cur = g ? g.actions : (pend ? pend.actions : ['read']);
        return `<li><form class="cx-stack" data-agent="${esc(a.agent_id)}" style="flex:1 1 100%">
          <span><b>@${esc(a.name)}</b> <span class="hd-ident">${esc(shortId(a.agent_id))}</span> ${g ? permPills(g.actions) : ''}${pend ? pill('Waiting for the room owner', 'var(--status-attention)') : ''}</span>
          <span class="cx-checks">${permBoxes(cur, 'p')}</span>${owner ? ttlSelect() : ''}
          <button type="submit" class="hd-btn hd-btn--sm">${owner ? (g ? 'Update permissions' : 'Add to room') : (g ? 'Request change' : 'Request access')}</button></form></li>`;
      }).join('')}</ul>${owner ? '' : '<p class="cx-msg">The room owner approves what each agent may do here.</p>'}` : '<div class="hd-empty">No agents yet. <a href="#/agents" data-go="/agents">Connect one</a>.</div>';
      box.querySelectorAll('[data-go]').forEach((el) => { el.onclick = (e) => { if (!standalone) { e.preventDefault(); go(el.dataset.go); } }; });
      box.querySelectorAll('form[data-agent]').forEach((f) => {
        f.onsubmit = async (ev) => {
          ev.preventDefault();
          const actions = picked(f, 'p');
          if (!actions.length) return;
          try { await api.grantAgent(roomId, f.dataset.agent, actions, expiryOf(f)); } catch (e) { alert(e.message); }
          await refreshAgentPanels();
        };
      });
    };

    const drawRequests = async () => {
      const box = $('hcn-requests');
      if (!box) return;
      const { requests } = await api.agentRequests(roomId);
      box.innerHTML = requests.length ? `<ul class="cx-list">${requests.map((r) => `<li><form class="cx-stack" data-req="${esc(r.request_id)}" style="flex:1 1 100%">
          <span><b>@${esc(r.name)}</b> from ${esc(r.owner_email || shortId(r.owner_user_id))}</span>
          <span class="cx-checks">${permBoxes(r.actions, 'p')}</span>${ttlSelect()}
          <span class="cx-row"><button type="submit" class="hd-btn hd-btn--sm" data-decision="approve">Approve</button><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-deny="1">Deny</button></span></form></li>`).join('')}</ul>` : '<div class="hd-empty">No requests.</div>';
      if (requests.length > 1) {
        box.insertAdjacentHTML('afterbegin', `<div class="cx-row"><button type="button" class="hd-btn hd-btn--sm" id="hcn-approve-all">Approve all ${requests.length}</button>${ttlSelect().replace('<select', '<select id="hcn-all-ttl"')}</div>`);
        $('hcn-approve-all').onclick = async () => {
          const v = Number($('hcn-all-ttl').value || 0);
          try { await api.approveAll(roomId, v ? { expires_at: Date.now() / 1000 + v } : {}); } catch (e) { alert(e.message); }
          await refreshAgentPanels();
        };
      }
      box.querySelectorAll('form[data-req]').forEach((f, i) => {
        const asked = requests[i].actions;
        f.querySelectorAll('input[name="p"]').forEach((c) => { if (!asked.includes(c.value)) { c.disabled = true; c.checked = false; } });   // cannot approve more than was asked
        f.onsubmit = async (ev) => {
          ev.preventDefault();
          const actions = picked(f, 'p');
          if (!actions.length) return;
          await api.decideRequest(roomId, f.dataset.req, 'approve', actions, expiryOf(f));
          await refreshAgentPanels();
        };
        f.querySelector('[data-deny]').onclick = async () => { await api.decideRequest(roomId, f.dataset.req, 'deny'); await refreshAgentPanels(); };
      });
    };

    const taskChip = (st) => pill(st, ({ done: 'var(--status-ok)', failed: 'var(--status-danger, #c0392b)', pending: 'var(--status-attention)', accepted: 'var(--accent)' })[st] || 'var(--text-muted)');
    const drawTasks = async () => {
      const box = $('hcn-tasks');
      if (!box) return;
      const { tasks } = await api.tasks(roomId);
      box.innerHTML = tasks.length ? `<ul class="cx-list">${tasks.slice(0, 20).map((t) => `<li><span class="grow"><b>@${esc(t.target_name || shortId(t.target_agent_id))}</b> ${esc(t.text)}${t.result ? `<br><span class="hd-ident">→ ${esc(t.result)}</span>` : ''}</span>${taskChip(t.status)}${(['pending', 'accepted'].includes(t.status) && canPost) ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-cancel-task="${esc(t.task_id)}">Cancel</button>` : ''}</li>`).join('')}</ul>` : '<div class="hd-empty">No tasks yet.</div>';
      box.querySelectorAll('[data-cancel-task]').forEach((b) => { b.onclick = async () => { try { await api.cancelTask(roomId, b.dataset.cancelTask); } catch (e) { alert(e.message); } drawTasks(); }; });
    };
    const drawTaskTargets = () => {
      const sel = $('hcn-task-target');
      if (!sel) return;
      const can = roomAgentList.filter((a) => a.actions.includes('execute'));
      sel.innerHTML = can.length ? can.map((a) => `<option value="${esc(a.agent_id)}">@${esc(a.name)}</option>`).join('') : '<option value="">No agent accepts tasks yet</option>';
    };
    const refreshAgentPanels = async () => {
      await Promise.all([drawRoomAgents(), drawMyAgents().catch(() => {}), canInvite ? drawRequests().catch(() => {}) : null, drawTasks().catch(() => {})]);
    };
    await refreshAgentPanels();
    clearInterval(timer2); timer2 = setInterval(refreshAgentPanels, 8000);
    const taskForm = $('hcn-task');
    if (taskForm) {
      taskForm.onsubmit = async (ev) => {
        ev.preventDefault();
        const f = new FormData(taskForm);
        try {
          await api.createTask(roomId, f.get('target'), f.get('text'));
          taskForm.reset(); $('hcn-task-msg').textContent = ''; drawTaskTargets(); await drawTasks(); await poll();
        } catch (e) { $('hcn-task-msg').textContent = e.message; }
      };
    }
    const sayForm = $('hcn-say');
    if (sayForm) {
      sayForm.onsubmit = async (ev) => {
        ev.preventDefault();
        const text = new FormData(sayForm).get('text');
        const mention = /^@([a-z0-9-]{1,32})\s+([\s\S]+)$/.exec(text.trim());
        try {
          if (mention) {   // "@helper do this" gives that agent a task instead of posting chat
            const hits = roomAgentList.filter((a) => a.name === mention[1] && a.actions.includes('execute'));
            if (hits.length !== 1) throw new Error(hits.length ? `More than one agent is called @${mention[1]}; use the Tasks panel` : `No agent named @${mention[1]} in this room accepts tasks`);
            const target = hits[0];
            await api.createTask(roomId, target.agent_id, mention[2]);
            sayForm.reset(); $('hcn-say-msg').textContent = ''; await drawTasks(); await poll(); log.scrollTop = log.scrollHeight;
            return;
          }
          await api.say(roomId, text); sayForm.reset(); $('hcn-say-msg').textContent = ''; await poll(); log.scrollTop = log.scrollHeight; } catch (e) { $('hcn-say-msg').textContent = e.message; }
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

  if (standalone) window.addEventListener('hashchange', onHash);
  start();
  return { dispose: () => { dispose(); window.removeEventListener('hashchange', onHash); } };
}

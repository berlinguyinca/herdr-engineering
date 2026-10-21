// Collaboration rooms: sign in with the BinView user pool, create rooms, chat, and invite
// people by email. All remote text is untrusted: it is escaped, never interpreted.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import {
  AuthError, CollabApi, TokenStore, authorizeUrl, exchangeCode, parseRedirect, pkcePair, randomString,
} from './herdr-web-client/collab.js';

const store = new TokenStore(window.sessionStorage);
const api = new CollabApi(() => store.get());
const ROLES = [['contributor', 'Can chat'], ['viewer', 'Read only'], ['guest', 'Guest (read only)']];

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

export function mountCollab(root) {
  let timer = null;
  let cursor = 0;
  const dispose = () => { clearInterval(timer); timer = null; };

  async function signIn(cfg) {
    const { verifier, challenge } = await pkcePair();
    const state = randomString(16);
    window.sessionStorage.setItem('hcn.pkce', JSON.stringify({ verifier, state }));
    window.location.assign(authorizeUrl(cfg, cfg.public_url, state, challenge));
  }

  let lastCfg = null;
  async function start() {
    const cfg = await loadConfig();
    lastCfg = cfg;
    if (!cfg) {
      root.innerHTML = '<h1>Collaboration</h1><p class="empty" role="status">The collaboration network is not enabled on this server.</p>';
      return;
    }
    if (window.location.origin !== new URL(cfg.public_url).origin) {
      root.innerHTML = `<h1>Collaboration</h1><p role="status">Collaboration is only available over HTTPS.
        <a href="${esc(cfg.public_url)}#/collab">Open it here</a>.</p>`;
      return;
    }
    const err = window.sessionStorage.getItem('hcn.error');
    window.sessionStorage.removeItem('hcn.error');
    if (!store.get()) {
      root.innerHTML = `<h1>Collaboration</h1>${err ? `<p role="alert">${esc(err)}</p>` : ''}
        <p>Sign in with your BinView account. You need to be in the <strong>local.dev</strong> group.</p>
        <button id="hcn-signin" type="button">Sign in</button>`;
      document.getElementById('hcn-signin').onclick = () => signIn(cfg);
      return;
    }
    try {
      const tokens = store.get();
      const me = await api.link(tokens.id);   // records your verified email so others can invite you
      await showRooms(me, cfg);
    } catch (e) {
      if (e instanceof AuthError) { store.clear(); return start(); }
      root.innerHTML = `<h1>Collaboration</h1><p role="alert">${esc(e.message)}</p>
        <p>If you were just added to the group, sign out and sign in again.</p>
        <button id="hcn-out" type="button">Sign out</button>`;
      document.getElementById('hcn-out').onclick = () => { store.clear(); start(); };
    }
  }

  async function showRooms(me, cfg = lastCfg) {
    dispose();
    const { rooms } = await api.rooms();
    root.innerHTML = `<h1>Collaboration</h1>
      <p>Signed in as <strong>${esc(me.email)}</strong> · <button id="hcn-out" type="button">Sign out</button></p>
      <h2>Your rooms</h2>
      ${rooms.length ? `<ul id="hcn-rooms">${rooms.map((r) => `<li><button type="button" data-room="${esc(r.room_id)}">${esc(r.name)}</button> <small>${esc(r.role)}</small></li>`).join('')}</ul>` : '<p class="empty">No rooms yet.</p>'}
      <h2>Your AI agents</h2><div id="hcn-agents"></div>
      <h2>New room</h2>
      <form id="hcn-new"><label>Room name <input name="name" required maxlength="120"></label>
        <button type="submit">Create</button> <span id="hcn-new-msg" role="status"></span></form>`;
    document.getElementById('hcn-out').onclick = () => { store.clear(); start(); };
    renderAgents(cfg);
    document.querySelectorAll('[data-room]').forEach((b) => { b.onclick = () => showRoom(me, b.dataset.room); });
    document.getElementById('hcn-new').onsubmit = async (ev) => {
      ev.preventDefault();
      const name = new FormData(ev.target).get('name');
      const msg = document.getElementById('hcn-new-msg');
      try {
        const p = await api.createProject(name);
        const r = await api.createRoom(p.project_id, name);
        showRoom(me, r.room_id);
      } catch (e) { msg.textContent = e.message; }
    };
  }

  const mcpCommand = (cfg, a) => `claude mcp add --transport http herdr-collab ${new URL('/mcp', cfg.public_url).href} `
    + `--header "Authorization: Agent ${a.credential}" --header "X-Herdr-Device: ${a.device_id}"`;

  async function renderAgents(cfg) {
    const box = document.getElementById('hcn-agents');
    if (!box) return;
    const { agents } = await api.agents();
    const live = agents.filter((a) => a.status === 'active');
    box.innerHTML = `${live.length ? `<ul>${live.map((a) => `<li>${esc(a.harness_type)} <small>${esc(a.agent_id)} · expires ${esc(new Date(a.expires_at * 1000).toLocaleDateString())} · ${a.rooms.length ? `in ${esc(a.rooms.map((r) => r.name).join(', '))}` : 'not in any room yet'}</small> <button type="button" data-revoke="${esc(a.agent_id)}">Revoke</button></li>`).join('')}</ul>` : '<p class="empty">No connected agents.</p>'}
      <button id="hcn-connect" type="button">Connect Claude Code or another MCP client</button>
      <div id="hcn-connect-out" role="status"></div>`;
    box.querySelectorAll('[data-revoke]').forEach((b) => { b.onclick = async () => { await api.revokeAgent(b.dataset.revoke); renderAgents(cfg); }; });
    document.getElementById('hcn-connect').onclick = async () => {
      const a = await api.connectAgent();
      const out = document.getElementById('hcn-connect-out');
      out.innerHTML = `<p>Run this once in a terminal. It is shown only now and acts as you in the rooms you add it to, so keep it private:</p>
        <pre style="white-space:pre-wrap;word-break:break-all"></pre>
        <p>Then add the agent to a room from that room's page. Revoke it here at any time.</p>`;
      out.querySelector('pre').textContent = mcpCommand(cfg, a);   // textContent: never parsed as HTML
    };
  }

  async function showRoom(me, roomId) {
    dispose();
    cursor = 0;
    let info;
    try { info = await api.room(roomId); } catch (e) { return showRooms(me); }
    const canInvite = info.role === 'owner';
    const canPost = ['owner', 'contributor'].includes(info.role);
    root.innerHTML = `<h1>${esc(info.name)}</h1>
      <p><button id="hcn-back" type="button">← Rooms</button> <small>You are ${esc(info.role)}</small></p>
      <div id="hcn-log" role="log" aria-live="polite" style="max-height:50vh;overflow:auto"></div>
      ${canPost ? '<form id="hcn-say"><input name="text" required maxlength="8000" aria-label="Message" style="width:70%"> <button type="submit">Send</button> <span id="hcn-say-msg" role="status"></span></form>' : '<p><small>Read-only access.</small></p>'}
      ${canPost ? '<h2>Your agents in this room</h2><div id="hcn-grant"></div>' : ''}
      ${canInvite ? `<h2>People</h2><div id="hcn-people"></div>
        <form id="hcn-invite"><label>Invite by email <input name="email" type="email" required></label>
          <label>Access <select name="role">${ROLES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}</select></label>
          <button type="submit">Invite</button> <span id="hcn-invite-msg" role="status"></span></form>` : ''}`;
    document.getElementById('hcn-back').onclick = () => showRooms(me);
    const log = document.getElementById('hcn-log');

    const poll = async () => {
      try {
        const page = await api.events(roomId, cursor);
        for (const e of page.events) {
          const row = document.createElement('div');
          if (e.event_type === 'message') {
            const who = e.sender.type === 'agent' ? `agent of ${e.sender.owner_user_id}` : e.sender.id;
            row.innerHTML = `<small>${esc(who)} · ${esc(new Date(e.created_at).toLocaleTimeString())}</small><div>${esc(e.text)}</div>`;
          } else {
            row.innerHTML = `<small><em>${esc(e.event_type.replaceAll('_', ' '))}</em></small>`;
          }
          log.appendChild(row);
        }
        if (page.events.length) { cursor = page.next_cursor; log.scrollTop = log.scrollHeight; }
      } catch (e) {
        if (e instanceof AuthError) { dispose(); store.clear(); start(); } else if (/not found/.test(e.message)) { dispose(); showRooms(me); }
      }
    };
    await poll();
    timer = setInterval(poll, 3000);

    const grantBox = document.getElementById('hcn-grant');
    if (grantBox) {
      const draw = async () => {
        const { agents } = await api.agents();
        const live = agents.filter((a) => a.status === 'active');
        grantBox.innerHTML = live.length ? `<ul>${live.map((a) => {
          const g = a.rooms.find((r) => r.room_id === roomId);
          return `<li>${esc(a.harness_type)} <small>${esc(a.agent_id)}</small> ${g
            ? `<small>${esc(g.actions.join('+'))}</small> <button type="button" data-g="${esc(a.agent_id)}" data-a="read">Make read-only</button>`
            : `<button type="button" data-g="${esc(a.agent_id)}" data-a="read">Add (read)</button> <button type="button" data-g="${esc(a.agent_id)}" data-a="read,post">Add (read + post)</button>`}</li>`;
        }).join('')}</ul>` : '<p class="empty">Connect an agent from the rooms page first.</p>';
        grantBox.querySelectorAll('[data-g]').forEach((b) => { b.onclick = async () => { await api.grantAgent(roomId, b.dataset.g, b.dataset.a.split(',')); draw(); }; });
      };
      await draw();
    }
    const sayForm = document.getElementById('hcn-say');
    if (sayForm) {
      sayForm.onsubmit = async (ev) => {
        ev.preventDefault();
        const text = new FormData(sayForm).get('text');
        try { await api.say(roomId, text); sayForm.reset(); await poll(); } catch (e) { document.getElementById('hcn-say-msg').textContent = e.message; }
      };
    }
    if (canInvite) {
      const people = async () => {
        const [room, inv] = await Promise.all([api.room(roomId), api.invites(roomId)]);
        document.getElementById('hcn-people').innerHTML = `<ul>${room.members.map((m) => `<li>${esc(m.email || m.user_id)} <small>${esc(m.role)}</small>${m.role !== 'owner' ? ` <button type="button" data-rm="${esc(m.user_id)}">Remove</button>` : ''}</li>`).join('')}
          ${inv.invites.map((i) => `<li>${esc(i.email)} <small>${esc(i.role)} · invitation pending</small> <button type="button" data-cancel="${esc(i.invite_id)}">Cancel</button></li>`).join('')}</ul>`;
        document.querySelectorAll('[data-rm]').forEach((b) => { b.onclick = async () => { await api.removeMember(roomId, b.dataset.rm); people(); }; });
        document.querySelectorAll('[data-cancel]').forEach((b) => { b.onclick = async () => { await api.cancelInvite(roomId, b.dataset.cancel); people(); }; });
      };
      await people();
      document.getElementById('hcn-invite').onsubmit = async (ev) => {
        ev.preventDefault();
        const f = new FormData(ev.target);
        const msg = document.getElementById('hcn-invite-msg');
        try {
          await api.invite(roomId, f.get('email'), f.get('role'));
          msg.textContent = 'Invited. They get access when they sign in here.';
          ev.target.reset(); people();
        } catch (e) { msg.textContent = e.message; }
      };
    }
  }

  start();
  return { dispose };
}

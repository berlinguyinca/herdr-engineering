// Collaboration network (HCN) browser client: Cognito Hosted-UI sign-in (authorization
// code + PKCE; the password never touches this page) and the /api/v1/collab wrapper.
// Pure functions with injected fetch/storage so they run under `node --test`.
const b64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes)))
  .replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');

export function randomString(bytes = 32, cryptoImpl = globalThis.crypto) {
  return b64url(cryptoImpl.getRandomValues(new Uint8Array(bytes)));
}

export async function pkcePair(cryptoImpl = globalThis.crypto) {
  const verifier = randomString(48, cryptoImpl);
  const digest = await cryptoImpl.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return { verifier, challenge: b64url(digest) };
}

export function authorizeUrl(cfg, redirectUri, state, challenge) {
  const q = new URLSearchParams({
    response_type: 'code', client_id: cfg.client_id, redirect_uri: redirectUri,
    scope: 'openid email', state, code_challenge: challenge, code_challenge_method: 'S256',
  });
  return `${cfg.hosted_ui_domain.replace(/\/$/, '')}/oauth2/authorize?${q}`;
}

export function parseRedirect(search) {
  const q = new URLSearchParams(search);
  if (q.get('error')) return { error: q.get('error_description') || q.get('error') };
  if (q.get('code') && q.get('state')) return { code: q.get('code'), state: q.get('state') };
  return null;
}

export async function exchangeCode(cfg, code, verifier, redirectUri, fetchFn = fetch) {
  const res = await fetchFn(`${cfg.hosted_ui_domain.replace(/\/$/, '')}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code', client_id: cfg.client_id, code,
      code_verifier: verifier, redirect_uri: redirectUri,
    }),
  });
  if (!res.ok) throw new Error('sign-in failed');
  const t = await res.json();
  if (!t.access_token || !t.id_token) throw new Error('sign-in returned no tokens');
  return { access: t.access_token, id: t.id_token };
}

export function tokenExpiry(jwt) {
  try {
    const payload = JSON.parse(atob(jwt.split('.')[1].replaceAll('-', '+').replaceAll('_', '/')));
    return typeof payload.exp === 'number' ? payload.exp * 1000 : 0;
  } catch { return 0; }
}

// Tokens live in sessionStorage only (cleared with the tab), never localStorage.
export class TokenStore {
  constructor(storage) { this.storage = storage; }
  get() {
    try {
      const t = JSON.parse(this.storage.getItem('hcn.tokens') || 'null');
      return t && tokenExpiry(t.access) > Date.now() + 15000 ? t : null;
    } catch { return null; }
  }
  set(tokens) { this.storage.setItem('hcn.tokens', JSON.stringify(tokens)); }
  clear() { this.storage.removeItem('hcn.tokens'); }
}

export class AuthError extends Error {}

export class CollabApi {
  constructor(getTokens, fetchFn = (...a) => fetch(...a), base = '/api/v1/collab') {
    this.getTokens = getTokens; this.fetch = fetchFn; this.base = base;
  }
  async call(method, path, body, signal) {
    const t = this.getTokens();
    if (!t) throw new AuthError('signed out');
    const res = await this.fetch(this.base + path, {
      method,
      headers: { Authorization: `Bearer ${t.access}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      signal,
    });
    if (res.status === 401) throw new AuthError('session expired');
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
    return data;
  }
  me() { return this.call('GET', '/me'); }
  link(idToken) { return this.call('POST', '/me', { id_token: idToken }); }
  rooms() { return this.call('GET', '/rooms'); }
  // approval: the people who asked to join (operator: everyone; approvers: those waiting)
  users(status) { return this.call('GET', `/admin/users${status ? `?status=${encodeURIComponent(status)}` : ''}`); }
  decideUser(user, action, reason) {
    return this.call('POST', `/admin/users/${encodeURIComponent(user)}/${encodeURIComponent(action)}`, reason ? { reason } : {});
  }
  userLog() { return this.call('GET', '/admin/users/log'); }
  room(id) { return this.call('GET', `/rooms/${encodeURIComponent(id)}`); }
  createProject(name) { return this.call('POST', '/projects', { name }); }
  createRoom(project, name) { return this.call('POST', `/projects/${encodeURIComponent(project)}/rooms`, { name }); }
  events(id, cursor) { return this.call('GET', `/rooms/${encodeURIComponent(id)}/events?cursor=${cursor}&limit=200`); }
  say(id, text) {
    return this.call('POST', `/rooms/${encodeURIComponent(id)}/messages`,
      { text, idempotency_key: `web-${Date.now().toString(36)}-${randomString(9)}` });
  }
  agents() { return this.call('GET', '/agents'); }
  connectAgent(name) {
    return this.call('POST', '/agents/connect', { harness_type: 'claude-code', ...(name ? { name } : {}) });
  }
  // One long-poll for everything a room shows: returns {changed:false} after `wait` seconds of quiet.
  state(room, since, cursor, wait, signal) {
    return this.call('GET', `/rooms/${encodeURIComponent(room)}/state?since=${since}&cursor=${cursor}&wait=${wait}`, undefined, signal);
  }
  flow(room) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/flow`); }
  linkAgent(agentId, instanceId) { return this.call('POST', `/agents/${encodeURIComponent(agentId)}/link`, { instance_id: instanceId || null }); }
  createJoinToken(body) { return this.call('POST', '/agents/join-tokens', body); }
  joinTokens() { return this.call('GET', '/agents/join-tokens'); }
  revokeJoinToken(id) { return this.call('POST', `/agents/join-tokens/${encodeURIComponent(id)}/revoke`, {}); }
  fleetHosts() { return this.call('GET', '/fleet/hosts'); }
  setFleetHost(host, autoAgents) { return this.call('POST', `/fleet/hosts/${encodeURIComponent(host)}`, { auto_agents: autoAgents }); }
  requestSpawn(body) { return this.call('POST', '/fleet/spawns', body); }
  spawns() { return this.call('GET', '/fleet/spawns'); }
  bulkConnect(body) { return this.call('POST', '/agents/bulk', body); }
  approveAll(room, body = {}) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/agent-requests/approve-all`, body); }
  renameAgent(id, name) { return this.call('POST', `/agents/${encodeURIComponent(id)}/rename`, { name }); }
  revokeAgent(id) { return this.call('POST', `/agents/${encodeURIComponent(id)}/revoke`, {}); }
  grantAgent(room, agentId, actions, expiresAt) {
    return this.call('POST', `/rooms/${encodeURIComponent(room)}/agents`,
      { agent_id: agentId, actions, ...(expiresAt ? { expires_at: expiresAt } : {}) });
  }
  roomAgents(id) { return this.call('GET', `/rooms/${encodeURIComponent(id)}/agents`); }
  agentRequests(id) { return this.call('GET', `/rooms/${encodeURIComponent(id)}/agent-requests`); }
  decideRequest(id, requestId, decision, actions, expiresAt) {
    return this.call('POST', `/rooms/${encodeURIComponent(id)}/agent-requests/${encodeURIComponent(requestId)}/decide`,
      { decision, ...(actions ? { actions } : {}), ...(expiresAt ? { expires_at: expiresAt } : {}) });
  }
  removeAgent(id, agentId) {
    return this.call('POST', `/rooms/${encodeURIComponent(id)}/agents/${encodeURIComponent(agentId)}/remove`, {});
  }
  tasks(id) { return this.call('GET', `/rooms/${encodeURIComponent(id)}/tasks`); }
  createTask(id, agentId, text) {
    return this.call('POST', `/rooms/${encodeURIComponent(id)}/tasks`, { target_agent_id: agentId, text });
  }
  cancelTask(id, taskId) {
    return this.call('POST', `/rooms/${encodeURIComponent(id)}/tasks/${encodeURIComponent(taskId)}/cancel`, {});
  }
  invite(id, email, role) { return this.call('POST', `/rooms/${encodeURIComponent(id)}/invites`, { email, role }); }
  invites(id) { return this.call('GET', `/rooms/${encodeURIComponent(id)}/invites`); }
  cancelInvite(id, inviteId) {
    return this.call('POST', `/rooms/${encodeURIComponent(id)}/invites/${encodeURIComponent(inviteId)}/cancel`, {});
  }
  removeMember(id, user) {
    return this.call('POST', `/rooms/${encodeURIComponent(id)}/members/${encodeURIComponent(user)}/remove`, {});
  }
}

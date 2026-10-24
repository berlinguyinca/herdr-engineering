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

const errorCode = (status) => ({ 401: 'unauthorized', 403: 'forbidden', 404: 'not_found',
  409: 'conflict', 429: 'rate_limited' })[status] || (status >= 500 ? 'unavailable' : 'request_failed');

export class CollabError extends Error {
  constructor(message, status = 0, code = errorCode(status)) {
    super(message);
    this.name = 'CollabError'; this.status = status; this.code = code;
  }
}

export class AuthError extends CollabError {
  constructor(message) { super(message, 401); this.name = 'AuthError'; }
}

// Both shells consume the same server decisions. Legacy responses lack the newer
// status fields; retain their role behavior, but never infer upload permission.
export function normalizeIdentity(data) {
  const status = data.user_status ?? data.status ?? 'approved';
  return { ...data, status, user_status: status, capabilities: {
    canManagePeople: status === 'approved' && (data.admin === true || data.approver === true),
  } };
}

export function normalizeRoom(data) {
  const knownRole = ['owner', 'contributor', 'viewer', 'guest'].includes(data.role);
  const userStatus = data.user_status ?? 'approved';
  const membership = data.room_membership_status ?? (knownRole ? 'approved' : 'none');
  const canRead = knownRole && userStatus === 'approved' && membership === 'approved';
  return { ...data, user_status: userStatus, room_membership_status: membership,
    agent_permission_status: data.agent_permission_status ?? {},
    file_upload_permission: data.file_upload_permission === true,
    capabilities: {
      canRead, canPost: canRead && ['owner', 'contributor'].includes(data.role),
      canInvite: canRead && data.role === 'owner', canUpload: canRead && data.file_upload_permission === true,
    } };
}

// `route` is the page; the decoded room ID is separate from URL/shell handling.
// Request state is independent of user, membership, file and upload statuses.
export function collabRouteState({ surface = 'public', route = '/rooms', status = 'idle', error } = {}) {
  const path = route.replace(/^#/, '');
  const room = /^\/room\/(.+)$/.exec(path);
  let roomId = null;
  if (room) {
    try { roomId = decodeURIComponent(room[1]); }
    catch { status = 'not-found'; }
  }
  if (error) status = ({ unauthorized: 'signed-out', forbidden: 'forbidden', not_found: 'not-found',
    unavailable: 'degraded', network_error: 'degraded', invalid_response: 'degraded', rate_limited: 'degraded',
  })[error.code] || 'error';
  return { surface, route: roomId ? '/room' : ['/agents', '/people'].includes(path) ? path : '/rooms', roomId, status };
}

export class CollabApi {
  constructor(getTokens, fetchFn = (...a) => fetch(...a), base = '/api/v1/collab') {
    this.getTokens = getTokens; this.fetch = fetchFn; this.base = base;
  }
  async call(method, path, body, signal) {
    const t = this.getTokens();
    if (!t) throw new AuthError('signed out');
    let res;
    try {
      res = await this.fetch(this.base + path, {
        method,
        headers: { Authorization: `Bearer ${t.access}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
        signal,
      });
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new CollabError('network request failed', 0, 'network_error');
    }
    if (res.status === 401) throw new AuthError('session expired');
    let data;
    try { data = await res.json(); }
    catch (error) {
      if (error.name === 'AbortError') throw error;
      if (res.ok) throw new CollabError('invalid server response', res.status, 'invalid_response');
    }
    if (!res.ok) throw new CollabError(typeof data?.error === 'string' ? data.error : `request failed (${res.status})`, res.status);
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      throw new CollabError('invalid server response', res.status, 'invalid_response');
    }
    return data;
  }
  async me() { return normalizeIdentity(await this.call('GET', '/me')); }
  async link(idToken) { return normalizeIdentity(await this.call('POST', '/me', { id_token: idToken })); }
  async rooms() {
    const data = await this.call('GET', '/rooms');
    if (!Array.isArray(data.rooms) || data.rooms.some((room) => !room || typeof room !== 'object' || Array.isArray(room))) {
      throw new CollabError('invalid server response', 200, 'invalid_response');
    }
    return { ...data, rooms: data.rooms.map(normalizeRoom) };
  }
  roomFiles(roomId, cursor) {
    return this.call('GET', `/rooms/${encodeURIComponent(roomId)}/files${cursor == null ? '' : `?cursor=${encodeURIComponent(cursor)}`}`);
  }
  uploadStatus(uploadId) { return this.call('GET', `/uploads/${encodeURIComponent(uploadId)}`); }
  skills(scope) { return this.call('GET', `/skills${scope == null ? '' : `?scope=${encodeURIComponent(scope)}`}`); }
  // approval: the people who asked to join (operator: everyone; approvers: those waiting)
  users(status) { return this.call('GET', `/admin/users${status ? `?status=${encodeURIComponent(status)}` : ''}`); }
  decideUser(user, action, reason) {
    return this.call('POST', `/admin/users/${encodeURIComponent(user)}/${encodeURIComponent(action)}`, reason ? { reason } : {});
  }
  // Lifecycle HTTP contracts: servers without these routes return visible errors.
  deletePerson(user) { return this.call('POST', `/admin/users/${encodeURIComponent(user)}/delete`, {}); }
  approveMember(room, user) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/members/${encodeURIComponent(user)}/approve`, {}); }
  setUploadPermission(room, user, allowed) {
    return this.call('POST', `/rooms/${encodeURIComponent(room)}/members/${encodeURIComponent(user)}/upload-permission`, { file_upload_permission: allowed });
  }
  archiveRoom(room) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/archive`, {}); }
  userLog() { return this.call('GET', '/admin/users/log'); }
  async room(id) { return normalizeRoom(await this.call('GET', `/rooms/${encodeURIComponent(id)}`)); }
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
  // agent helpers: the room profile (names only, never secret values), host-access requests, the panic switch
  roomProfile(id) { return this.call('GET', `/rooms/${encodeURIComponent(id)}/profile`); }
  setRoomProfile(id, profile) { return this.call('POST', `/rooms/${encodeURIComponent(id)}/profile`, profile); }
  egressRequests() { return this.call('GET', '/fleet/egress'); }
  decideEgress(requestId, decision, ttlSeconds) {
    return this.call('POST', `/fleet/egress/${encodeURIComponent(requestId)}/decide`,
      { decision, ...(ttlSeconds ? { ttl_seconds: ttlSeconds } : {}) });
  }
  promoteEgress(room, hostPort) { return this.call('POST', '/fleet/egress/promote', { room_id: room, host_port: hostPort }); }
  removeEgress(room, hostPort) { return this.call('POST', '/fleet/egress/remove', { room_id: room, host_port: hostPort }); }
  egressAllowlist(room) { return this.call('GET', `/fleet/egress/${encodeURIComponent(room)}`); }
  helpers() { return this.call('GET', '/fleet/helpers'); }
  myHelpers() { return this.call('GET', '/helpers'); }
  summaryFixHelpers() { return this.call('GET', '/fleet/summary-fix/helpers'); }   // operator: results of summary-fix helpers (untrusted text)
  fleetCancelHelper(id) { return this.call('POST', `/fleet/helpers/${encodeURIComponent(id)}/cancel`, {}); }
  cancelHelper(id) { return this.call('POST', `/helpers/${encodeURIComponent(id)}/cancel`, {}); }
  pauseHelpers(paused) { return this.call('POST', '/fleet/helpers/pause', { paused: Boolean(paused) }); }
}

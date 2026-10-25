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

function lineageQuery(room, o, extra = {}) {
  const q = new URLSearchParams({ room_id: room });
  if (o.step !== null && o.step !== undefined) q.set('step', String(o.step));
  else if (o.at) q.set('at', String(o.at));
  for (const [k, v] of Object.entries(extra)) if (v !== undefined && v !== null && v !== '' && v !== 0) q.set(k, String(v));
  return q;
}

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
    if (!res.ok) {
      const err = new Error(data.error || `request failed (${res.status})`);
      err.status = res.status; err.code = data.code; err.fence = data.fence;
      throw err;
    }
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
  say(id, text, context, files) {
    return this.call('POST', `/rooms/${encodeURIComponent(id)}/messages`,
      { text, idempotency_key: `web-${Date.now().toString(36)}-${randomString(9)}`, ...(context ? { context } : {}),
        ...(files?.length ? { files } : {}) });
  }
  // Room files (C5): uploads are raw bytes (never a form), downloads come back as a Blob to save, never to display.
  async rawCall(method, path, body) {
    const t = this.getTokens();
    if (!t) throw new AuthError('signed out');
    const res = await this.fetch(this.base + path, {
      method, headers: { Authorization: `Bearer ${t.access}`, ...(body !== undefined ? { 'Content-Type': 'application/octet-stream' } : {}) }, body,
    });
    if (res.status === 401) throw new AuthError('session expired');
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      const err = new Error(data.error || `request failed (${res.status})`);
      err.status = res.status; err.code = data.code;
      throw err;
    }
    return res;
  }
  files(room) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/files`); }
  async uploadFile(room, file, parent) {
    const r = encodeURIComponent(room);
    const path = parent ? `/rooms/${r}/files/${encodeURIComponent(parent)}/versions` : `/rooms/${r}/files?name=${encodeURIComponent(file.name)}`;
    return (await (await this.rawCall('POST', path, file)).json()).file;
  }
  async downloadFile(room, id, version) {
    const res = await this.rawCall('GET', `/rooms/${encodeURIComponent(room)}/files/${encodeURIComponent(id)}${version ? `?v=${version}` : ''}`);
    return res.blob();
  }
  deleteFile(room, id) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/files/${encodeURIComponent(id)}/delete`, {}); }
  // Consultation (T14): a question and ONE text answer. Cross-workspace links (T14): both ends are checked by the server.
  attention(room, includeAcked = false) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/attention${includeAcked ? '?include=acked' : ''}`); }
  attentionAck(room, body) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/attention/ack`, body); }
  attentionSnooze(room, body) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/attention/snooze`, body); }
  attentionRestore(room, body) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/attention/restore`, body); }
  progress(room) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/progress`); }
  analytics(room) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/analytics`); }
  consultations(room) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/consultations`); }
  askConsultation(room, body) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/consultations`, body); }
  answerConsultation(room, id, body) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/consultations/${encodeURIComponent(id)}/answer`, body); }
  cancelConsultation(room, id) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/consultations/${encodeURIComponent(id)}/cancel`, {}); }
  xlinks(room) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/xlinks`); }
  createLink(room, body) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/xlinks`, body); }
  revokeLink(room, id) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/xlinks/${encodeURIComponent(id)}/revoke`, {}); }
  agents() { return this.call('GET', '/agents'); }
  connectAgent(name) {
    return this.call('POST', '/agents/connect', { harness_type: 'claude-code', ...(name ? { name } : {}) });
  }
  // One long-poll for everything a room shows: returns {changed:false} after `wait` seconds of quiet.
  state(room, since, cursor, wait, signal) {
    return this.call('GET', `/rooms/${encodeURIComponent(room)}/state?since=${since}&cursor=${cursor}&wait=${wait}`, undefined, signal);
  }
  flow(room) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/flow`); }
  // Nodes of one type this person may see in ONE room (server-filtered by membership), with the visible
  // edges of `edgeType` between them. The room's own scope and its linked missions only.
  graphList(room, nodeType, edgeType) {
    const q = new URLSearchParams({ node_type: nodeType, room_id: room, limit: '500', ...(edgeType ? { edge_type: edgeType } : {}) });
    return this.call('GET', `/graph/nodes?${q}`);
  }
  // External coworker / remote agents (T10). The invite code is returned once by inviteExternal.
  externals(room) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/externals`); }
  inviteExternal(room, body) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/externals/invites`, body); }
  revokeExternal(room, agentId) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/externals/${encodeURIComponent(agentId)}/revoke`, {}); }
  revokeExternalInvite(room, inviteId) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/externals/invites/${encodeURIComponent(inviteId)}/revoke`, {}); }
  // Bounded delegation and artifact proposals (T11). Owner side only; a proposal's content is untrusted text.
  delegations(room) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/delegations`); }
  delegate(room, body) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/delegations`, body); }
  cancelDelegation(room, taskId) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/tasks/${encodeURIComponent(taskId)}/cancel`, {}); }
  proposals(room) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/proposals`); }
  proposal(room, id) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/proposals/${encodeURIComponent(id)}`); }
  decideProposal(room, id, body) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/proposals/${encodeURIComponent(id)}/decide`, body); }
  // Read-only lineage views of ONE room (T09). `step` replays the reader's own visible ledger up to that event;
  // omitted = now. The server filters by the caller's CURRENT membership.
  lineage(room, o = {}) {
    return this.call('GET', `/graph/lineage?${lineageQuery(room, o, { limit: '200', offset: o.offset, node_types: o.nodeTypes, edge_types: o.edgeTypes })}`);
  }
  lineageTimeline(room, o = {}) { return this.call('GET', `/graph/lineage/timeline?${lineageQuery(room, o)}`); }
  lineageEvents(room, o = {}) {
    return this.call('GET', `/graph/lineage/events?${lineageQuery(room, { step: null }, { limit: String(o.limit || 200), cursor: o.cursor })}`);
  }
  // Comments anchored to graph nodes and explicit steering (T12). Identity, room scope and rights come from the
  // credential and the room; the body never names an author, an owner or a target principal.
  comments(room, o = {}) {
    const a = o.anchor || (o.nodeId ? { kind: 'node', node_id: o.nodeId } : null);
    const q = new URLSearchParams({ limit: '100', ...(a ? Object.fromEntries(Object.entries({ anchor_kind: a.kind, node_id: a.node_id, edge_type: a.edge_type, from_id: a.from_id, to_id: a.to_id }).filter(([, v]) => v)) : {}) });
    return this.call('GET', `/rooms/${encodeURIComponent(room)}/comments?${q}`);
  }
  addComment(room, body) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/comments`, body); }
  editComment(room, id, text, revision) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/comments/${encodeURIComponent(id)}/edit`, { text, expected_revision: revision }); }
  retractComment(room, id, revision) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/comments/${encodeURIComponent(id)}/retract`, { expected_revision: revision }); }
  commentHistory(room, id) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/comments/${encodeURIComponent(id)}/history`); }
  steeringTarget(room, nodeId) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/steering/target?${new URLSearchParams({ node_id: nodeId })}`); }
  forks(room, nodeId) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/forks?${new URLSearchParams({ node_id: nodeId })}`); }
  steeringList(room, nodeId) { return this.call('GET', `/rooms/${encodeURIComponent(room)}/steering${nodeId ? `?${new URLSearchParams({ node_id: nodeId })}` : ''}`); }
  proposeSteering(room, body) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/steering`, body); }
  confirmSteering(room, id, body) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/steering/${encodeURIComponent(id)}/confirm`, body); }
  rejectSteering(room, id, reason) { return this.call('POST', `/rooms/${encodeURIComponent(room)}/steering/${encodeURIComponent(id)}/reject`, reason ? { reason } : {}); }
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

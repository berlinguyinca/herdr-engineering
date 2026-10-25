// herdr-web-client — structured API + live-event client (Spec §45, §52, §92).
//
// The web UI is a *client*: it consumes the same structured /api/v1 JSON and
// SSE event stream as the rest of the platform. No UI scraping of terminals,
// Docker, Slurm, or HTML. fetch/EventSource are injected so this is fully
// unit-testable with `node --test` (no browser required).

export class ApiError extends Error {
  constructor(status, body, message) {
    super(message || `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

export class HerdrClient {
  constructor(baseUrl = '', { fetchImpl = fetch } = {}) {
    this.base = baseUrl.replace(/\/+$/, '');
    this.fetch = fetchImpl;
  }

  async request(path, { method = 'GET', body, signal } = {}) {
    const url = `${this.base}${path}`;
    const headers = { Accept: 'application/json' };
    let payload;
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      payload = JSON.stringify(body);
    }
    // Call fetch detached: the native Window.fetch throws "Illegal
    // invocation" if it is invoked as a method (receiver = this client),
    // so extract it to a local first (this === undefined).
    const doFetch = this.fetch;
    const res = await doFetch(url, { method, headers, body: payload, signal });
    const text = await res.text();
    let json;
    try {
      json = text ? JSON.parse(text) : null;
    } catch {
      json = { raw: text };
    }
    if (!res.ok) {
      throw new ApiError(res.status, json,
        (json && json.error) || `HTTP ${res.status}`);
    }
    return json;
  }

  // Fleet config revision/drift status; null when the server has no fleet config (404).
  // Bounded (default 5 s) so a slow status endpoint cannot hold up the Hosts page.
  async getFleetConfigStatus({ signal, timeoutMs = 5000 } = {}) {
    const timeout = AbortSignal.timeout(timeoutMs);
    let bounded = timeout;
    if (signal && AbortSignal.any) bounded = AbortSignal.any([signal, timeout]);
    else if (signal) {   // older runtimes: honour the caller's signal too
      const ctl = new AbortController();
      for (const s of [signal, timeout]) {
        if (s.aborted) ctl.abort(s.reason); else s.addEventListener('abort', () => ctl.abort(s.reason), { once: true });
      }
      bounded = ctl.signal;
    }
    try {
      return await this.request('/api/v1/fleet/config/status', { signal: bounded });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) return null;
      throw err;
    }
  }

  // -- missions -----------------------------------------------------------
  listMissions(status, { signal } = {}) {
    const q = status ? `?status=${encodeURIComponent(status)}` : '';
    return this.request(`/api/v1/missions${q}`, { signal });
  }

  getMission(id) {
    return this.request(`/api/v1/missions/${encodeURIComponent(id)}`);
  }

  missionTests(id, { signal } = {}) {
    return this.request(`/api/v1/missions/${encodeURIComponent(id)}/tests`, { signal });
  }

  missionBuilds(id, { signal } = {}) {
    return this.request(`/api/v1/missions/${encodeURIComponent(id)}/builds`, { signal });
  }

  missionReviews(id, { signal } = {}) {
    return this.request(`/api/v1/missions/${encodeURIComponent(id)}/reviews`, { signal });
  }

  missionPulls(id, { signal } = {}) {
    return this.request(`/api/v1/missions/${encodeURIComponent(id)}/pulls`, { signal });
  }

  listMissionActivity({ signal } = {}) {
    return this.request('/api/v1/mission-activity', { signal });
  }

  missionActivity(id, { signal } = {}) {
    return this.request(`/api/v1/missions/${encodeURIComponent(id)}/activity`, { signal });
  }

  missionTimeline(id) {
    return this.request(`/api/v1/missions/${encodeURIComponent(id)}/timeline`);
  }

  // -- sessions -----------------------------------------------------------
  listSessions(status, { signal } = {}) {
    const q = status ? `?status=${encodeURIComponent(status)}` : '';
    return this.request(`/api/v1/sessions${q}`, { signal });
  }

  getSession(id, { signal } = {}) {
    return this.request(`/api/v1/sessions/${encodeURIComponent(id)}`, { signal });
  }

  getAgentSummary(id, { signal } = {}) {
    return this.request(`/api/v1/sessions/${encodeURIComponent(id)}/summary`, { signal });
  }

  regenerateAgentSummary(id) {
    return this.request(`/api/v1/sessions/${encodeURIComponent(id)}/summary`, { method: 'POST', body: {} });
  }

  // Hand one stored blocker/next step back to the agent that owns the summary.
  // The server re-checks `text` against the stored item and dedupes replays.
  fixSummaryItem(id, list, index, text) {
    return this.request(`/api/v1/sessions/${encodeURIComponent(id)}/summary/fix`, {
      method: 'POST', body: { list, index, text },
    });
  }

  // Terminal button: the server builds the attach command from the session record; the
  // viewer runs it in their own terminal. Nothing is sent but the session id.
  getTerminalAttach(id) {
    return this.request(`/api/v1/sessions/${encodeURIComponent(id)}/terminal`, { method: 'POST', body: {} });
  }

  renameAgent(id, name, expectedVersion) {
    return this.request(`/api/v1/sessions/${encodeURIComponent(id)}/name`, {
      method: 'POST', body: { name, expected_version: expectedVersion },
    });
  }

  // -- services / hosts ---------------------------------------------------
  listServices({ signal } = {}) {
    return this.request('/api/v1/services', { signal });
  }

  listHosts({ signal } = {}) {
    return this.request('/api/v1/hosts', { signal });
  }

  // -- activity / analytics ------------------------------------------------
  activity(limit = 50, { signal } = {}) {
    return this.request(`/api/v1/activity?limit=${limit}`, { signal });
  }

  /** LLM page statistics. `params` are the query filters (window, group, project, model, ...). */
  llm(what, params = {}) {
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null && v !== ''));
    return this.request(`/api/v1/llm/${encodeURIComponent(what)}${q.size ? `?${q}` : ''}`);
  }

  /** Stored outcome summaries keyed by mission key, for the mission catalog rows. */
  missionOutcomes({ signal } = {}) {
    return this.request('/api/v1/mission-outcomes', { signal });
  }

  /** One mission's story (header, needs, events, agents, outcome, usage). Rejects with status 404 when unknown. */
  missionStory(key, { signal } = {}) {
    return this.request(`/api/v1/missions/${encodeURIComponent(key)}/story`, { signal });
  }

  /** Neighbours of one entity (cross-linking). Rejects with status 404 for an unknown entity. */
  related(kind, id, { signal } = {}) {
    const q = new URLSearchParams({ kind, id });
    return this.request(`/api/v1/related?${q}`, { signal });
  }

  /** Fleet attention inbox (derived server-side; ack/snooze are shared by every browser). */
  attention(params = {}, { signal } = {}) {
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null && v !== ''));
    return this.request(`/api/v1/attention${q.size ? `?${q}` : ''}`, { signal });
  }

  attentionAct(action, body) {
    if (!['ack', 'snooze', 'clear'].includes(action)) throw new Error(`unknown attention action: ${action}`);
    return this.request(`/api/v1/attention/${action}`, { method: 'POST', body });
  }

  analytics(kind = 'missions') {
    return this.request(`/api/v1/analytics/${encodeURIComponent(kind)}`);
  }

  // -- interaction ----------------------------------------------------------
  sendMessage(sessionId, message, artifactIds = [], operationId) {
    return this.request(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
      { method: 'POST', body: { message, artifact_ids: artifactIds, ...(operationId ? {operation_id: operationId} : {}) } });
  }

  sessionAction(sessionId, action) {
    return this.request(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/actions`,
      { method: 'POST', body: { action } });
  }

  listCollabAgentLinks() {
    return this.request('/api/v1/collab/fleet/agent-links');
  }

  // -- agent inspector (spec 0200) -----------------------------------------
  getInspector(sessionId, { view = 'all', q, agentId, since_rev, limit, before_seq } = {}) {
    const p = new URLSearchParams({ view });
    if (q) p.set('q', q);
    if (agentId) p.set('agent_id', agentId);
    if (since_rev) p.set('since_rev', String(since_rev));
    if (limit) p.set('limit', String(limit));
    if (before_seq != null) p.set('before_seq', String(before_seq));
    return this.request(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/inspector?${p}`);
  }

  getInspectorEvent(sessionId, seq) {
    return this.request(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/inspector/events/${seq}`);
  }

  getInspectorPrefs(sessionId) {
    return this.request(sessionId
      ? `/api/v1/sessions/${encodeURIComponent(sessionId)}/inspector/preferences`
      : '/api/v1/inspector/preferences');
  }

  setInspectorPrefs(sessionId, prefs) {
    return this.request(sessionId
      ? `/api/v1/sessions/${encodeURIComponent(sessionId)}/inspector/preferences`
      : '/api/v1/inspector/preferences', { method: 'POST', body: prefs });
  }

  inspectorStreamUrl(sessionId) {
    return `${this.base}/api/v1/sessions/${encodeURIComponent(sessionId)}/inspector/stream`;
  }

  // -- GitHub project and issue inventory ---------------------------------
  getGithub({ page, per_page, search, signal } = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries({ page, per_page, search })) {
      if (value != null) query.set(key, String(value));
    }
    return this.request(`/api/v1/github${query.size ? `?${query}` : ''}`, { signal });
  }

  getGithubIssues(repository, { tab, search, page, per_page, signal } = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries({ repository, tab, search, page, per_page })) {
      if (value != null && !(key === 'repository' && value === '')) query.set(key, String(value));
    }
    return this.request(`/api/v1/github/issues${query.size ? `?${query}` : ''}`, { signal });
  }

  getGithubIssue(repository, number, { signal } = {}) {
    const query = new URLSearchParams({ repository, number: String(number) });
    return this.request(`/api/v1/github/issue?${query}`, { signal });
  }

  // -- architecture explorer (immutable commit-keyed snapshots) ----------------
  _arch(path, params = {}, signal) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v != null && v !== '') q.set(k, String(v));
    return this.request(`/api/v1/architecture/${path}${q.size ? `?${q}` : ''}`, { signal });
  }
  listArchitectureSnapshots({ signal, ...params } = {}) { return this._arch('snapshots', params, signal); }
  listArchitectureAnalyses({ signal, ...params } = {}) { return this._arch('analyses', params, signal); }
  getArchitectureChildren(sid, { signal, ...params } = {}) { return this._arch(`snapshots/${encodeURIComponent(sid)}/children`, params, signal); }
  searchArchitectureNodes(sid, { signal, ...params } = {}) { return this._arch(`snapshots/${encodeURIComponent(sid)}/nodes`, params, signal); }
  getArchitectureNode(sid, nid, { signal } = {}) { return this._arch(`snapshots/${encodeURIComponent(sid)}/nodes/${encodeURIComponent(nid)}`, {}, signal); }
  expandArchitectureNode(sid, nid, { signal, ...params } = {}) { return this._arch(`snapshots/${encodeURIComponent(sid)}/nodes/${encodeURIComponent(nid)}/edges`, params, signal); }
  diffArchitecture(base, head, { signal, ...params } = {}) { return this._arch('diff', { base, head, ...params }, signal); }
  getArchitectureOverlays(sid, { signal } = {}) { return this._arch(`snapshots/${encodeURIComponent(sid)}/overlays`, {}, signal); }
  getArchitectureCorrelations(sid, { signal, ...params } = {}) { return this._arch(`snapshots/${encodeURIComponent(sid)}/correlations`, params, signal); }
  getArchitectureNodeRooms(sid, nid, { signal } = {}) { return this._arch(`snapshots/${encodeURIComponent(sid)}/nodes/${encodeURIComponent(nid)}/rooms`, {}, signal); }
  getArchitectureTimeline(repositoryId, { signal, ...params } = {}) { return this._arch('timeline', { repository_id: repositoryId, ...params }, signal); }
  getArchitectureCrossRepo(snapshotIds, mappings = [], { signal } = {}) {
    const q = new URLSearchParams();
    for (const id of snapshotIds) q.append('snapshot', id);
    for (const m of mappings) q.append('map', `${m.importer}|${m.package}|${m.provider}`);
    return this.request(`/api/v1/architecture/cross-repo?${q}`, { signal });
  }
  requestArchitectureAnalysis(body, { signal } = {}) {
    return this.request('/api/v1/architecture/analyses/request', { method: 'POST', body, signal });
  }
  cancelArchitectureAnalysis(analysisId, { signal } = {}) {
    return this.request(`/api/v1/architecture/analyses/${encodeURIComponent(analysisId)}/cancel`, { method: 'POST', body: {}, signal });
  }
  explainArchitectureNode(sid, nid, { signal } = {}) {
    return this.request(`/api/v1/architecture/snapshots/${encodeURIComponent(sid)}/nodes/${encodeURIComponent(nid)}/explain`, { method: 'POST', body: {}, signal });
  }

  // -- ci / test runs (leaf types for the topology view) -------------------
  getCi({ signal } = {}) {
    return this.request('/api/v1/ci', { signal });
  }

  getCiReports({ signal } = {}) {
    return this.request('/api/v1/ci/reports', { signal });
  }

  getCiSummary({ days = 14, repository, branch, scope, provider_url, provider_repo_id, signal } = {}) {
    const query = new URLSearchParams({ days: String(days) });
    for (const [key, value] of Object.entries({ repository, branch, scope, provider_url, provider_repo_id })) {
      if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
    }
    return this.request(`/api/v1/ci/summary?${query}`, { signal });
  }

  getCiTests({ days = 14, repository, branch, test, provider_url, provider_repo_id, signal } = {}) {
    const query = new URLSearchParams({ days: String(days) });
    for (const [key, value] of Object.entries({ repository, branch, test, provider_url, provider_repo_id })) {
      if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
    }
    return this.request(`/api/v1/ci/tests?${query}`, { signal });
  }

  getCiPerformance({ days = 14, repository, branch, signal } = {}) {
    const query = new URLSearchParams({ days: String(days) });
    for (const [key, value] of Object.entries({ repository, branch })) {
      if (value) query.set(key, value);
    }
    return this.request(`/api/v1/ci/performance?${query}`, { signal });
  }

  getCiHistory({ signal } = {}) {
    return this.request('/api/v1/ci/history', { signal });
  }

  getCiPipeline(repository, number, { signal, providerUrl } = {}) {
    const query = new URLSearchParams({ repository, number: String(number), ...(providerUrl!==undefined?{provider_url:providerUrl}:{}) });
    return this.request(`/api/v1/ci/pipeline?${query}`, { signal });
  }

  getCiLog(repository, number, step, { signal, providerUrl } = {}) {
    const query = new URLSearchParams({ repository, number: String(number), step: String(step), ...(providerUrl!==undefined?{provider_url:providerUrl}:{}) });
    return this.request(`/api/v1/ci/log?${query}`, { signal });
  }

  ciAction(repository, number, action, { signal, providerUrl } = {}) {
    return this.request('/api/v1/ci/actions', {
      method: 'POST', body: { repository, number, action, ...(providerUrl!==undefined?{provider_url:providerUrl}:{}) }, signal,
    });
  }

  debugCi(repository, number, sessionId, operationId, { signal, providerUrl, mode } = {}) {
    return this.request('/api/v1/ci/debug', {
      method: 'POST', body: { repository, number, session_id: sessionId, operation_id: operationId, ...(providerUrl!==undefined?{provider_url:providerUrl}:{}), ...(mode==='fix'?{mode}:{}) }, signal,
    });
  }

  // Sandboxed CI fix agent. The control plane only records the request; a runner on a host does the work.
  ciFix(repository, number, operationId, { signal, providerUrl } = {}) {
    return this.request('/api/v1/ci/fix', {
      method: 'POST', body: { repository, number, operation_id: operationId, ...(providerUrl!==undefined?{provider_url:providerUrl}:{}) }, signal,
    });
  }

  listCiFix({ run, signal } = {}) {
    const query = new URLSearchParams(run ? { run } : {});
    return this.request(`/api/v1/ci/fix${query.size ? `?${query}` : ''}`, { signal });
  }

  getCiFix(id, { signal } = {}) {
    return this.request(`/api/v1/ci/fix/${encodeURIComponent(id)}`, { signal });
  }

  cancelCiFix(id, { signal } = {}) {
    return this.request(`/api/v1/ci/fix/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: {}, signal });
  }

  getReview(id, { signal } = {}) {
    return this.request(`/api/v1/reviews/${encodeURIComponent(id)}`, { signal });
  }

  listReviews({ signal } = {}) {
    return this.request('/api/v1/reviews', { signal });
  }

  getTests() {
    return this.request('/api/v1/tests');
  }

  // -- health / self-observability -----------------------------------------
  health({ signal } = {}) {
    return this.request('/api/v1/health', { signal });
  }
}

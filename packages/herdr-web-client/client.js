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

const CONDITIONAL_ENTRIES = 64;

export class HerdrClient {
  constructor(baseUrl = '', { fetchImpl = fetch } = {}) {
    this.base = baseUrl.replace(/\/+$/, '');
    this.fetch = fetchImpl;
    // URL -> { etag, text } for conditional GETs, least recently used first.
    this.conditional = new Map();
  }

  get conditionalSize() { return this.conditional.size; }

  // Conditional GET. A response that carried an ETag is remembered (by URL, at most
  // CONDITIONAL_ENTRIES) and the next read sends If-None-Match. A 304 answers
  // { body, notModified: true } with the remembered body, parsed afresh so a caller that
  // mutates its response cannot change what the next caller gets. A 304 for a body this
  // client no longer holds is retried once without the tag.
  async getConditional(path, { signal } = {}) {
    const url = `${this.base}${path}`;
    const doFetch = this.fetch;
    for (let attempt = 0; attempt < 2; attempt++) {
      const held = attempt === 0 ? this.conditional.get(url) : undefined;
      const headers = { Accept: 'application/json' };
      if (held) headers['If-None-Match'] = held.etag;
      const res = await doFetch(url, { method: 'GET', headers, signal });
      if (res.status === 304) {
        if (!held) { this.conditional.delete(url); continue; }
        this.conditional.delete(url); this.conditional.set(url, held);
        return { body: JSON.parse(held.text), notModified: true };
      }
      const text = await res.text();
      let json;
      try { json = text ? JSON.parse(text) : null; } catch { json = { raw: text }; }
      if (!res.ok) {
        throw new ApiError(res.status, json, (json && json.error) || `HTTP ${res.status}`);
      }
      const etag = res.headers?.get?.('ETag');
      this.conditional.delete(url);
      if (etag && text) {
        this.conditional.set(url, { etag, text });
        while (this.conditional.size > CONDITIONAL_ENTRIES) this.conditional.delete(this.conditional.keys().next().value);
      }
      return { body: json, notModified: false };
    }
    throw new ApiError(304, null, 'Not modified, but no remembered body');
  }

  async getJson(path, { signal } = {}) {
    return (await this.getConditional(path, { signal })).body;
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
    return this.getJson(`/api/v1/missions${q}`, { signal });
  }

  getMission(id, { signal } = {}) {
    return this.getJson(`/api/v1/missions/${encodeURIComponent(id)}`, { signal });
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
    return this.getJson(`/api/v1/sessions${q}`, { signal });
  }

  getSession(id, { signal } = {}) {
    return this.getJson(`/api/v1/sessions/${encodeURIComponent(id)}`, { signal });
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
  // The polled list and detail reads below send If-None-Match (the API answers 304 when unchanged).
  listServices({ signal } = {}) {
    return this.getJson('/api/v1/services', { signal });
  }

  getService(id, { signal } = {}) {
    return this.getJson(`/api/v1/services/${encodeURIComponent(id)}`, { signal });
  }

  listHosts({ signal } = {}) {
    return this.getJson('/api/v1/hosts', { signal });
  }

  getHost(id, { signal } = {}) {
    return this.getJson(`/api/v1/hosts/${encodeURIComponent(id)}`, { signal });
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
    return this.getGithubConditional('repositories', { page, per_page, search, signal }).then((r) => r.body);
  }

  getGithubIssues(repository, { tab, search, page, per_page, signal } = {}) {
    return this.getGithubConditional('issues', { repository, tab, search, page, per_page, signal }).then((r) => r.body);
  }

  // One GitHub list read as { body, notModified }. `since` (a revision the caller holds) asks for a
  // delta: { revision, resync:false, upserted, removed }, or { resync:true, <full list> }.
  getGithubConditional(kind, { repository, tab, search, page, per_page, since, signal } = {}) {
    const query = new URLSearchParams();
    const params = kind === 'issues' ? { repository, tab, search, page, per_page, since } : { page, per_page, search, since };
    for (const [key, value] of Object.entries(params)) {
      if (value != null && !(key === 'repository' && value === '')) query.set(key, String(value));
    }
    const path = kind === 'issues' ? '/api/v1/github/issues' : '/api/v1/github';
    return this.getConditional(`${path}${query.size ? `?${query}` : ''}`, { signal });
  }

  // GitHub Projects v2 (read-only): the in-scope projects, or one project's items with `project`.
  // Same contract as getGithubConditional: { body, notModified }, `since` asks for a delta.
  getGithubProjects({ project, since, signal } = {}) {
    const query = new URLSearchParams();
    if (project) query.set('project', String(project));
    if (since != null) query.set('since', String(since));
    return this.getConditional(`/api/v1/github/projects${query.size ? `?${query}` : ''}`, { signal });
  }

  getGithubIssue(repository, number, { signal } = {}) {
    const query = new URLSearchParams({ repository, number: String(number) });
    return this.request(`/api/v1/github/issue?${query}`, { signal });
  }

  // -- ci / test runs (leaf types for the topology view) -------------------
  getCi({ signal } = {}) {
    return this.request('/api/v1/ci', { signal });
  }

  getCiReports({ signal } = {}) {
    return this.request('/api/v1/ci/reports', { signal });
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

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

  // -- ci / test runs (leaf types for the topology view) -------------------
  getCi({ signal } = {}) {
    return this.request('/api/v1/ci', { signal });
  }

  getCiReports({ signal } = {}) {
    return this.request('/api/v1/ci/reports', { signal });
  }

  getCiHistory({ signal } = {}) {
    return this.request('/api/v1/ci/history', { signal });
  }

  getCiPipeline(repository, number, { signal } = {}) {
    const query = new URLSearchParams({ repository, number: String(number) });
    return this.request(`/api/v1/ci/pipeline?${query}`, { signal });
  }

  getCiLog(repository, number, step, { signal } = {}) {
    const query = new URLSearchParams({ repository, number: String(number), step: String(step) });
    return this.request(`/api/v1/ci/log?${query}`, { signal });
  }

  ciAction(repository, number, action, { signal } = {}) {
    return this.request('/api/v1/ci/actions', {
      method: 'POST', body: { repository, number, action }, signal,
    });
  }

  debugCi(repository, number, sessionId, operationId, { signal } = {}) {
    return this.request('/api/v1/ci/debug', {
      method: 'POST', body: { repository, number, session_id: sessionId, operation_id: operationId }, signal,
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

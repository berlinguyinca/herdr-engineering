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
  constructor(baseUrl = '', { fetchImpl = fetch, headers = {} } = {}) {
    this.base = baseUrl.replace(/\/+$/, '');
    this.fetch = fetchImpl;
    this.headers = { ...headers };
  }

  async request(path, { method = 'GET', body, signal } = {}) {
    const startedAt = Date.now();
    const url = `${this.base}${path}`;
    const headers = { ...this.headers, Accept: 'application/json' };
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
      const error = new ApiError(res.status, json,
        (json && json.error) || `HTTP ${res.status}`);
      error.requestId = res.headers?.get?.('X-Request-ID') || '';
      error.elapsedMs = Date.now() - startedAt;
      throw error;
    }
    if (json && typeof json === 'object' && !Array.isArray(json)) {
      Object.defineProperty(json, '_requestMeta', {value: {
        requestId: res.headers?.get?.('X-Request-ID') || '',
        elapsedMs: Date.now() - startedAt,
      }, enumerable: false});
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

  getHandoffTree({ signal } = {}) {
    return this.request('/api/v1/handoffs/tree', { signal });
  }

  listHandoffs(state, { signal } = {}) {
    const query = state ? `?state=${encodeURIComponent(state)}` : '';
    return this.request(`/api/v1/handoffs${query}`, { signal });
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

  // -- services / hosts ---------------------------------------------------
  listServices({ signal } = {}) {
    return this.request('/api/v1/services', { signal });
  }

  listArtifacts({ q, type, status, offset, limit, signal } = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries({ q, type, status, offset, limit })) {
      if (value != null && value !== '') query.set(key, String(value));
    }
    return this.request(`/api/v1/artifacts${query.size ? `?${query}` : ''}`, { signal });
  }

  getArtifact(id, { signal } = {}) {
    return this.request(`/api/v1/artifacts/${encodeURIComponent(id)}`, { signal });
  }

  artifactDownloadUrl(id) {
    return `${this.base}/api/v1/artifacts/${encodeURIComponent(id)}/download`;
  }

  artifactVersions(id, { signal, summary = false } = {}) {
    return this.request(`/api/v1/artifacts/${encodeURIComponent(id)}/versions${summary ? '?summary=true' : ''}`, { signal });
  }

  artifactVersion(id, versionId, { signal } = {}) {
    return this.request(`/api/v1/artifacts/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}`, { signal });
  }

  artifactReviews(id, { signal } = {}) {
    return this.request(`/api/v1/artifacts/${encodeURIComponent(id)}/reviews`, { signal });
  }

  requestArtifactReview(artifactId, versionId, { signal } = {}) {
    return this.request('/api/v1/artifact-reviews', {
      method: 'POST', body: { artifact_id: artifactId, version_id: versionId }, signal,
    });
  }

  completeArtifactReview(reviewId, decision, comments = [], { signal } = {}) {
    return this.request(`/api/v1/artifact-reviews/${encodeURIComponent(reviewId)}/complete`, {
      method: 'POST', body: { decision, comments }, signal,
    });
  }

  artifactReport(id, versionId, { signal } = {}) {
    return this.request(`${this.artifactReportPath(id, versionId)}/report`, { signal });
  }

  artifactReportPath(id, versionId) {
    return `/api/v1/artifacts/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}`;
  }

  artifactReportDiff(id, versionId, baseVersionId, { signal } = {}) {
    return this.request(`${this.artifactReportPath(id, versionId)}/diff?base_version_id=${encodeURIComponent(baseVersionId)}`, { signal });
  }

  artifactReportFeedback(id, versionId, { signal, limit, offset } = {}) {
    const query = new URLSearchParams();
    if (limit != null) query.set('limit', String(limit));
    if (offset != null) query.set('offset', String(offset));
    return this.request(`${this.artifactReportPath(id, versionId)}/feedback${query.size ? `?${query}` : ''}`, { signal });
  }

  addArtifactReportFeedback(id, versionId, body, { signal } = {}) {
    return this.request(`${this.artifactReportPath(id, versionId)}/feedback`, { method: 'POST', body, signal });
  }

  recordArtifactReportCheck(id, versionId, body, { signal } = {}) {
    return this.request(`${this.artifactReportPath(id, versionId)}/checks`, { method: 'POST', body, signal });
  }

  artifactReportAssetUrl(id, versionId, assetId) {
    return `${this.base}${this.artifactReportPath(id, versionId)}/assets/${encodeURIComponent(assetId)}`;
  }

  artifactReportPreview(id, versionId, { signal } = {}) {
    return this.artifactReportContent(`${this.artifactReportPath(id, versionId)}/preview`, 'text', signal);
  }

  downloadArtifactReportAsset(id, versionId, assetId, { signal } = {}) {
    return this.artifactReportContent(`${this.artifactReportPath(id, versionId)}/assets/${encodeURIComponent(assetId)}`, 'blob', signal);
  }

  async artifactReportContent(path, kind, signal) {
    const doFetch = this.fetch;
    const response = await doFetch(`${this.base}${path}`, {
      headers: { ...this.headers, Accept: kind === 'text' ? 'text/html' : 'application/octet-stream' }, signal,
    });
    if (!response.ok) {
      const text = await response.text();
      let body;
      try { body = JSON.parse(text); } catch { body = { raw: text }; }
      const error = new ApiError(response.status, body, body?.error || `HTTP ${response.status}`);
      error.requestId = response.headers?.get?.('X-Request-ID') || '';
      throw error;
    }
    return kind === 'text' ? response.text() : response.blob();
  }

  listPublications({ signal } = {}) {
    return this.request('/api/v1/publications', { signal });
  }

  approvePublication(id, { signal } = {}) {
    return this.request(`/api/v1/publications/${encodeURIComponent(id)}/approve`, {
      method: 'POST', body: {}, signal,
    });
  }

  revokePublication(id, { signal } = {}) {
    return this.request(`/api/v1/publications/${encodeURIComponent(id)}/revoke`, {
      method: 'POST', body: {}, signal,
    });
  }

  listHosts({ signal } = {}) {
    return this.request('/api/v1/hosts', { signal });
  }

  schedulerOverview({ signal } = {}) {
    return this.request('/api/v1/scheduler', { signal });
  }

  schedulerPlan(request, { signal } = {}) {
    return this.request('/api/v1/scheduler/plan', { method: 'POST', body: request, signal });
  }

  setSchedulerHostPolicy(policy) {
    return this.request('/api/v1/scheduler/host-policy', {method: 'POST', body: policy});
  }

  retrySchedulerReservation(reservationId) {
    return this.request('/api/v1/scheduler/reservations/retry', {
      method:'POST', body:{reservation_id:reservationId},
    });
  }

  previewSchedulerSimulation(body) {
    return this.request('/api/v1/scheduler/simulator', {method:'POST', body});
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

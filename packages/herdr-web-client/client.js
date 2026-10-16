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

  async request(path, { method = 'GET', body } = {}) {
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
    const res = await doFetch(url, { method, headers, body: payload });
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

  // -- missions -----------------------------------------------------------
  listMissions(status) {
    const q = status ? `?status=${encodeURIComponent(status)}` : '';
    return this.request(`/api/v1/missions${q}`);
  }

  getMission(id) {
    return this.request(`/api/v1/missions/${encodeURIComponent(id)}`);
  }

  missionTimeline(id) {
    return this.request(`/api/v1/missions/${encodeURIComponent(id)}/timeline`);
  }

  // -- sessions -----------------------------------------------------------
  listSessions(status) {
    const q = status ? `?status=${encodeURIComponent(status)}` : '';
    return this.request(`/api/v1/sessions${q}`);
  }

  getSession(id) {
    return this.request(`/api/v1/sessions/${encodeURIComponent(id)}`);
  }

  // -- services / hosts ---------------------------------------------------
  listServices() {
    return this.request('/api/v1/services');
  }

  listHosts() {
    return this.request('/api/v1/hosts');
  }

  // -- activity / analytics ------------------------------------------------
  activity(limit = 50) {
    return this.request(`/api/v1/activity?limit=${limit}`);
  }

  analytics(kind = 'missions') {
    return this.request(`/api/v1/analytics/${encodeURIComponent(kind)}`);
  }

  // -- interaction ----------------------------------------------------------
  sendMessage(sessionId, message, artifactIds = []) {
    return this.request(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
      { method: 'POST', body: { message, artifact_ids: artifactIds } });
  }

  sessionAction(sessionId, action) {
    return this.request(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/actions`,
      { method: 'POST', body: { action } });
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

  // -- ci / test runs (leaf types for the topology view) -------------------
  getCi() {
    return this.request('/api/v1/ci');
  }

  getTests() {
    return this.request('/api/v1/tests');
  }

  // -- health / self-observability -----------------------------------------
  health() {
    return this.request('/api/v1/health');
  }
}

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
    const res = await this.fetch(url, { method, headers, body: payload });
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

  // -- health / self-observability -----------------------------------------
  health() {
    return this.request('/api/v1/health');
  }
}

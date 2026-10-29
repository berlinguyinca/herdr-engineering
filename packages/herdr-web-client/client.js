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
  constructor(baseUrl = '', { fetchImpl = fetch, user = 'operator', tenant = 'default', headers = {} } = {}) {
    this.base = baseUrl.replace(/\/+$/, '');
    this.fetch = fetchImpl;
    this.user = user;
    this.tenant = tenant;
    this.headers = { ...headers };  // extra headers (e.g. API key) sent on every request
    this.conditional = new Map();  // url -> { etag, text } (bounded by CONDITIONAL_ENTRIES)
  }

  get conditionalSize() { return this.conditional.size; }

  async getConditional(path, { signal } = {}) {
    const url = `${this.base}${path}`;
    const doFetch = this.fetch;
    for (let attempt = 0; attempt < 2; attempt++) {
      const held = attempt === 0 ? this.conditional.get(url) : undefined;
      const headers = { ...this.headers, Accept: 'application/json' };
      if (held) headers['If-None-Match'] = held.etag;
      const res = await doFetch(url, { method: 'GET', headers, signal });
      const current = () => this.conditional.get(url) === held;
      if (res.status === 304) {
        if (!held) continue;
        if (current()) { this.conditional.delete(url); this.conditional.set(url, held); }
        return { body: JSON.parse(held.text), notModified: true };
      }
      const text = await res.text();
      let json;
      try { json = text ? JSON.parse(text) : null; } catch { json = { raw: text }; }
      if (!res.ok) {
        throw new ApiError(res.status, json, (json && json.error) || `HTTP ${res.status}`);
      }
      const etag = res.headers?.get?.('ETag');
      if (!current()) return { body: json, notModified: false };
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

  async request(path, { method = 'GET', body, signal = AbortSignal.timeout(15000) } = {}) {
    const startedAt = Date.now();
    const url = `${this.base}${path}`;
    const headers = { ...this.headers, Accept: 'application/json', 'X-Herdr-User': this.user, 'X-Herdr-Tenant': this.tenant };
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
      const retryAfter = res.headers?.get?.('Retry-After');
      if (retryAfter != null && retryAfter.trim() !== '') {
        const seconds = Number(retryAfter);
        const delay = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(retryAfter) - Date.now();
        if (Number.isFinite(delay)) error.retryAfterMs = Math.max(0, delay);
      }
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

  getHandoffTree({ signal } = {}) {
    return this.request('/api/v1/handoffs/tree', { signal });
  }

  listHandoffs(state, { signal } = {}) {
    const query = state ? `?state=${encodeURIComponent(state)}` : '';
    return this.request(`/api/v1/handoffs${query}`, { signal });
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
      headers: { ...this.headers, 'X-Herdr-User': this.user, 'X-Herdr-Tenant': this.tenant, Accept: kind === 'text' ? 'text/html' : 'application/octet-stream' }, signal,
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

  schedulerOverview({ signal } = {}) {
    return this.request('/api/v1/scheduler', { signal });
  }

  schedulerPlan(request, { signal } = {}) {
    return this.request('/api/v1/scheduler/plan', { method: 'POST', body: request, signal });
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

  // Start the full /summary report for one session; the result arrives as `full_report` on getAgentSummary.
  startSummaryReport(id) {
    return this.request(`/api/v1/sessions/${encodeURIComponent(id)}/summary/report`, { method: 'POST', body: {} });
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

  /** Canonical fabric URLs for URLs in agent output (read-only; originals are never changed). */
  resolveFabricUrls(urls, sourceMachine = null, { signal } = {}) {
    return this.request('/api/v1/fabric/resolve-urls', { method: 'POST', body: { urls, source_machine: sourceMachine }, signal });
  }

  /** Names that mark a URL as the fabric's (public fleet name, legacy aliases). */
  getFabricNames({ signal } = {}) {
    return this.request('/api/v1/fabric/names', { signal });
  }

  listFabricServices({ signal } = {}) {
    return this.request('/api/v1/fabric/services', { signal });
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
  getArchitectureBase(sid, { signal } = {}) { return this._arch(`snapshots/${encodeURIComponent(sid)}/base`, {}, signal); }
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

  getCiReports({ signal, compact = false, provider_url, provider_repo_id, repository, run_number } = {}) {
    const query = new URLSearchParams();
    if (compact) query.set('compact', '1');
    for (const [key, value] of Object.entries({provider_url, provider_repo_id, repository, run_number})) {
      if (value !== undefined && value !== null) query.set(key, String(value));
    }
    return this.request(`/api/v1/ci/reports${query.size ? `?${query}` : ''}`, { signal });
  }

  getCiSummary({ days = 14, repository, branch, scope, start, end, mode, run_number, provider_url, provider_repo_id, signal } = {}) {
    const query = new URLSearchParams(mode==='runs'||mode==='hours'?{}:{days:String(days??14)});
    for (const [key, value] of Object.entries({ repository, branch, scope, start, end, mode, run_number, provider_url, provider_repo_id })) {
      if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
    }
    return this.request(`/api/v1/ci/summary?${query}`, { signal });
  }

  getCiTests({ days = 14, repository, branch, test, scope, start, end, mode, run_number, provider_url, provider_repo_id, signal } = {}) {
    const query = new URLSearchParams(mode==='runs'||mode==='hours'?{}:{days:String(days??14)});
    for (const [key, value] of Object.entries({ repository, branch, test, scope, start, end, mode, run_number, provider_url, provider_repo_id })) {
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

  // -- Herdr Personal Assistant (Rakazo integration) -----------------------
  listAssistants({ signal } = {}) {
    return this.request('/api/v1/assistants', { signal });
  }
  createAssistant(body, { signal } = {}) {
    return this.request('/api/v1/assistants', { method: 'POST', body, signal });
  }
  getAssistant(id, { signal } = {}) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(id)}`, { signal });
  }
  updateAssistant(id, patch, { signal } = {}) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(id)}`, { method: 'PATCH', body: patch, signal });
  }
  archiveAssistant(id) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }
  listAssistantThreads(assistantId, { signal } = {}) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(assistantId)}/threads`, { signal });
  }
  createThread(assistantId, title, { signal } = {}) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(assistantId)}/threads`, { method: 'POST', body: { title }, signal });
  }
  getThread(assistantId, threadId, { signal } = {}) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(assistantId)}/threads/${encodeURIComponent(threadId)}`, { signal });
  }
  sendAssistantMessage(assistantId, threadId, content) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(assistantId)}/threads/${encodeURIComponent(threadId)}/messages`, { method: 'POST', body: { content } });
  }
  getRun(runId) {
    return this.request(`/api/v1/assistant-runs/${encodeURIComponent(runId)}`);
  }
  cancelRun(runId) {
    return this.request(`/api/v1/assistant-runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST', body: {} });
  }
  getAssistantPermissions(assistantId, { signal } = {}) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(assistantId)}/permissions`, { signal });
  }
  grantTool(assistantId, body, { signal } = {}) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(assistantId)}/permissions`, { method: 'POST', body, signal });
  }
  listApprovals({ signal } = {}) {
    return this.request('/api/v1/assistant-approvals', { signal });
  }
  decideApproval(approvalId, decision) {
    return this.request(`/api/v1/assistant-approvals/${encodeURIComponent(approvalId)}/decision`, { method: 'POST', body: { decision } });
  }
  requestToolAction(runId, body) {
    return this.request(`/api/v1/assistant-runs/${encodeURIComponent(runId)}/tool`, { method: 'POST', body });
  }
  searchMemory(assistantId, query, { signal } = {}) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(assistantId)}/memory?q=${encodeURIComponent(query)}`, { signal });
  }
  storeMemory(assistantId, body) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(assistantId)}/memory`, { method: 'POST', body });
  }
  exportMemory(assistantId) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(assistantId)}/memory/export`, { method: 'POST', body: {} });
  }
  forgetMemory(memoryId) {
    return this.request(`/api/v1/assistant-memory/${encodeURIComponent(memoryId)}`, { method: 'DELETE' });
  }
  assistantDiagnostics(assistantId, { signal } = {}) {
    return this.request(`/api/v1/assistants/${encodeURIComponent(assistantId)}/diagnostics`, { signal });
  }
  listRoutines({ signal } = {}) {
    return this.request('/api/v1/assistant-routines', { signal });
  }
  createRoutine(body, { signal } = {}) {
    return this.request('/api/v1/assistant-routines', { method: 'POST', body, signal });
  }
  triggerRoutine(routineId) {
    return this.request(`/api/v1/assistant-routines/${encodeURIComponent(routineId)}/trigger`, { method: 'POST', body: {} });
  }
  archiveRoutine(routineId) {
    return this.request(`/api/v1/assistant-routines/${encodeURIComponent(routineId)}`, { method: 'DELETE' });
  }
  correctMemory(memoryId, content) {
    return this.request(`/api/v1/assistant-memory/${encodeURIComponent(memoryId)}`, { method: 'PATCH', body: { content } });
  }
  listConnectors() {
    return this.request('/api/v1/assistant-connectors');
  }
  registerConnector(body) {
    return this.request('/api/v1/assistant-connectors', { method: 'POST', body });
  }
  archiveConnector(connectorId) {
    return this.request(`/api/v1/assistant-connectors/${encodeURIComponent(connectorId)}`, { method: 'DELETE' });
  }
  // The dashboard's connector check: the server opens its own bookkeeping run and takes the normal tool gate, so the
  // answer is a result, a refusal, or a 403 ``approval_needed`` (with the approval id and the dry-run text).
  testConnector(connectorId, body) {
    return this.request(`/api/v1/assistant-connectors/${encodeURIComponent(connectorId)}/test`, { method: 'POST', body });
  }
  executeApproval(approvalId) {
    return this.request(`/api/v1/assistant-approvals/${encodeURIComponent(approvalId)}/execute`, { method: 'POST', body: {} });
  }
  listProposals(status) {
    const q = status ? `?status=${encodeURIComponent(status)}` : '';
    return this.request(`/api/v1/assistant-proposals${q}`);
  }
  previewProposal(proposalId) {
    return this.request(`/api/v1/assistant-proposals/${encodeURIComponent(proposalId)}/preview`);
  }
  decideProposal(proposalId, decision) {
    return this.request(`/api/v1/assistant-proposals/${encodeURIComponent(proposalId)}/decision`, { method: 'POST', body: { decision } });
  }
  executeProposal(proposalId) {
    return this.request(`/api/v1/assistant-proposals/${encodeURIComponent(proposalId)}/execute`, { method: 'POST', body: {} });
  }
  listNotifications(state) {
    const q = state ? `?state=${encodeURIComponent(state)}` : '';
    return this.request(`/api/v1/assistant-notifications${q}`);
  }
  ackNotification(notificationId) {
    return this.request(`/api/v1/assistant-notifications/${encodeURIComponent(notificationId)}/ack`, { method: 'POST', body: {} });
  }
  connectorRequest(connectorId, body) {
    return this.request(`/api/v1/assistant-connectors/${encodeURIComponent(connectorId)}/request`, { method: 'POST', body });
  }
  listDelegations() {
    return this.request('/api/v1/assistant-delegations');
  }
  delegate(body) {
    return this.request('/api/v1/assistant-delegations', { method: 'POST', body });
  }
  cancelDelegation(delegationId) {
    return this.request(`/api/v1/assistant-delegations/${encodeURIComponent(delegationId)}/cancel`, { method: 'POST', body: {} });
  }
  registerDelegationArtifact(delegationId, artifactRef) {
    return this.request(`/api/v1/assistant-delegations/${encodeURIComponent(delegationId)}/artifact`, { method: 'POST', body: { artifact_ref: artifactRef } });
  }
}

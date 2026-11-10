// /api/v1 fixtures: core (attention, activity, events, handoffs, memory, intelligence, architecture, collab, related).
// See ../dashboard-fixtures.mjs for the contract. Every timestamp comes from ctx.ago(); nothing is random.
const SHA = ['3f9a1c07d2b84e5a96c0b1d7e8f3a24c5b6d7e80', '9b2e4d61a7c3f058e1d2c4b6a8f09e7d3c5b1a42', 'c4d8e2f6a0b1c3d5e7f9a1b3c5d7e9f1a3b5c7d9'];
const LONG = 'The reviewer asked for the retry policy to be moved out of the transport layer and into the mission supervisor, ' +
  'because two call sites now disagree about whether a 429 counts against the attempt budget. '.repeat(2).trim();

// ---------------------------------------------------------------- attention
function attention(params, ctx) {
  const ago = ctx.ago;
  const act = (label, href) => ({ label, href });
  const all = [
    { key: 'a1', fingerprint: 'fp-a1', kind: 'mission_failed', severity: 'critical', state: 'open', title: 'Mission M-302 failed at the verify stage',
      detail: 'pytest exited 1 on tests/test_gateway.py::test_retry_budget; 3 of 212 tests failed.', source: 'missions', href: '#/missions/M-302',
      first_seen: ago(5400), last_seen: ago(300), count: 3, actions: [act('Open mission', '#/missions/M-302'), act('Open session', '#/sessions/s-102')], handoffs: [], snoozed_until: null },
    { key: 'a2', fingerprint: 'fp-a2', kind: 'ci_failed', severity: 'critical', state: 'open', title: 'CI run 412 failed on org/repo',
      detail: 'lint job: 14 errors in packages/overview-view.js', source: 'ci', href: '#/ci/412',
      first_seen: ago(3600), last_seen: ago(3600), count: 1, actions: [act('Open run', '#/ci/412')], handoffs: [], snoozed_until: null },
    { key: 'a3', fingerprint: 'fp-a3', kind: 'service_down', severity: 'critical', state: 'open', title: 'Service svc-2 is down',
      detail: 'health check returned 503 for 12 minutes', source: 'services', href: '#/services/svc-2',
      first_seen: ago(900), last_seen: ago(60), count: 12, actions: [act('Open service', '#/services/svc-2')], handoffs: [], snoozed_until: null },
    { key: 'a4', fingerprint: 'fp-a4', kind: 'host_stale', severity: 'warning', state: 'open', title: 'Host nibbler has not reported for 2 h',
      detail: '', source: 'hosts', href: '#/hosts/nibbler',
      first_seen: ago(7500), last_seen: ago(7200), count: 1, actions: [act('Open host', '#/hosts/nibbler')], handoffs: [], snoozed_until: null },
    { key: 'a5', fingerprint: 'fp-a5', kind: 'review_changes', severity: 'warning', state: 'open', title: 'Review on PR 7 requested changes', detail: LONG,
      source: 'reviews', href: '#/reviews', first_seen: ago(14400), last_seen: ago(14000), count: 1, actions: [act('Open review', '#/reviews')],
      handoffs: [{ agent: 'pi:implementer', direction: 'out', goal: 'Address review comments on PR 7', state: 'in_progress', href: '#/handoffs?handoff=h-4' }], snoozed_until: null },
    { key: 'a6', fingerprint: 'fp-a6', kind: 'agent_blocked', severity: 'warning', state: 'open', title: 'Agent s-103 is waiting for input',
      detail: 'Asked: which branch should the migration target?', source: 'sessions', href: '#/sessions/s-103',
      first_seen: ago(2400), last_seen: ago(120), count: 1, actions: [act('Open session', '#/sessions/s-103')], handoffs: [], snoozed_until: null },
    { key: 'a7', fingerprint: 'fp-a7', kind: 'llm_problems', severity: 'info', state: 'open', title: '4 LLM requests were retried on bender', detail: 'upstream 529 overloaded',
      source: 'llm', href: '#/llm', first_seen: ago(1800), last_seen: ago(600), count: 4, actions: [], handoffs: [], snoozed_until: null },
    { key: 'a8', fingerprint: 'fp-a8', kind: 'fleet_drift', severity: 'info', state: 'acknowledged', title: 'Fleet drift: zapp runs an older herdr agent',
      detail: 'zapp 0.41.2, fleet 0.42.0', source: 'fleet', href: '#/fleet', first_seen: ago(86400), last_seen: ago(3000), count: 1, actions: [act('Open fleet', '#/fleet')], handoffs: [], snoozed_until: null },
    { key: 'a9', fingerprint: 'fp-a9', kind: 'mission_attention', severity: 'warning', state: 'snoozed', title: 'Mission M-304 needs a decision',
      detail: 'Two plans scored equally; pick one.', source: 'missions', href: '#/missions/M-304', first_seen: ago(43200), last_seen: ago(43000), count: 1,
      actions: [act('Open mission', '#/missions/M-304')], handoffs: [], snoozed_until: ago(-7200) },
  ];
  const st = params.get('state') || 'open';
  const items = st === 'all' ? all : all.filter(i => i.state === st);
  const open = all.filter(i => i.state === 'open');
  return {
    items, counts: { open: open.length, critical: open.filter(i => i.severity === 'critical').length, acknowledged: 1, snoozed: 1 },
    unavailable: [], partial: false, truncated: false,
    expected_off: [{ name: 'zapp', last_seen: ago(172800) }],
    notify: { min_severity: 'warning', quiet_hours: { start: '22:00', end: '07:00' } },
    resolved: [{ title: 'CI run 409 failed on org/repo', kind: 'ci_failed', resolved_at: ago(5000) }, { title: 'Host bender went stale', kind: 'host_stale', resolved_at: ago(20000) }],
  };
}

// ---------------------------------------------------------------- activity and events
function envelopes(ctx) {
  const ago = ctx.ago;
  const e = (n, type, entity_type, entity_id, secs, payload, mission_id = null) =>
    ({ event_id: `ev-${n}`, event_type: type, entity_type, entity_id, actor: { id: entity_type === 'host' ? entity_id : 'pi:supervisor' }, source_timestamp: ago(secs), mission_id, payload });
  return [
    e(1, 'MissionStarted', 'mission', 'M-301', 120, { title: 'Add retry budget to the gateway client', stage: 'plan' }, 'M-301'),
    e(2, 'SessionStarted', 'session', 's-101', 240, { agent_role: 'implementer', host_id: 'beast', mission_id: 'M-301' }, 'M-301'),
    e(3, 'HostHeartbeat', 'host', 'bender', 300, { host_name: 'bender' }),
    e(4, 'MissionUpdated', 'mission', 'M-302', 600, { title: 'Fix flaky overview poller', stage: 'verify', status: 'failed' }, 'M-302'),
    e(5, 'SessionTerminated', 'session', 's-102', 660, { agent_role: 'verifier', host_id: 'bender', status: 'failed' }, 'M-302'),
    e(6, 'HostUnavailable', 'host', 'nibbler', 7200, { host_name: 'nibbler' }),
    e(7, 'ServiceRegistered', 'service', 'svc-1', 9000, { service_name: 'herdr-gateway', url: 'http://beast:8080' }),
    e(8, 'ArtifactRegistered', 'artifact', 'art-1', 12000, { filename: 'verification-report.html' }, 'M-303'),
    e(9, 'HostRegistered', 'host', 'zapp', 90000, { host_name: 'zapp', tailnet_ip: '100.64.0.23' }),
    e(10, 'MissionEnded', 'mission', 'M-303', 14000, { title: 'Document the attention contract', status: 'completed' }, 'M-303'),
    e(11, 'CIPipelineFinished', 'ci', 'org/repo#411', 20000, { status: 'success' }),
    e(12, 'MissionStarted', 'mission', 'M-304', 44000, { title: '', stage: 'plan' }, 'M-304'),
  ];
}

// diagnostics-source.js readPage(): exactly {id, missionKey, at (ms), eventType, hostId?, sessionId?, payload?} per row.
function diagnosticsEvents(ctx) {
  const t = secs => ctx.now - secs * 1000;
  const row = (id, mission, secs, eventType, extra = {}) => ({ id, missionKey: mission, at: t(secs), eventType, ...extra });
  return {
    items: [
      row('d1', 'M-302', 1800, 'MissionStarted', { hostId: 'bender' }),
      row('d2', 'M-302', 1700, 'SessionStarted', { hostId: 'bender', sessionId: 's-102', payload: { step: 'plan' } }),
      row('d3', 'M-302', 1500, 'ToolCallStarted', { hostId: 'bender', sessionId: 's-102', payload: { step: 'edit packages/overview-view.js' } }),
      row('d4', 'M-302', 1300, 'ToolCallFailed', { hostId: 'bender', sessionId: 's-102', payload: { step: 'pytest -q', input_error: 'exit status 1' } }),
      row('d5', 'M-302', 1100, 'SessionTerminated', { hostId: 'bender', sessionId: 's-102', payload: { step: 'verify' } }),
      row('d6', 'M-301', 900, 'MissionStarted', { hostId: 'beast' }),
      row('d7', 'M-301', 800, 'SessionStarted', { hostId: 'beast', sessionId: 's-101', payload: { step: 'plan' } }),
      row('d8', 'M-301', 500, 'ToolCallStarted', { hostId: 'beast', sessionId: 's-101', payload: { step: 'read docs/contracts/gateway.md' } }),
      row('d9', 'M-303', 7000, 'MissionEnded', { hostId: 'zapp' }),
    ],
    truncated: false,
  };
}

function eventsFor(params, ctx) {
  if (!params.get('entity_type') && !params.get('entity_id')) return diagnosticsEvents(ctx);
  const type = params.get('entity_type'), id = params.get('entity_id');
  const limit = Number(params.get('limit') || 50);
  const own = envelopes(ctx).filter(ev => ev.entity_type === type && ev.entity_id === id);
  return { events: (own.length ? own : envelopes(ctx).slice(0, 3).map(ev => ({ ...ev, entity_type: type, entity_id: id }))).slice(0, limit) };
}

// ---------------------------------------------------------------- handoffs
function handoffRows(ctx) {
  const ago = ctx.ago;
  const r = (id, state, ws, goal, secs, extra = {}) => ({
    handoff_id: id, state, workspace: ws, source: { harness: 'claude', agent_id: 'claude:planner', session_id: 's-101' },
    target: { harness: 'pi', agent_id: 'pi:implementer', session_id: 's-102', model_hint: 'qwen3-coder' },
    goal, mission_id: 'M-301', updated_at: ago(secs), created_at: ago(secs + 1800), completed: 2, remaining: 1, attention: false, stalled: false,
    attention_reason: '', origin: 'registry', ...extra,
  });
  return [
    r('h-1', 'in_progress', 'org/repo', 'Implement the retry budget in the gateway client', 300),
    r('h-2', 'failed', 'org/repo', 'Port the overview poller to the new activity feed', 900, { attention: true, attention_reason: 'target failed', remaining: 3, completed: 0 }),
    r('h-3', 'completed', 'org/repo', 'Write the contract notes for /api/v1/attention', 3600, { remaining: 0, completed: 4 }),
    r('h-4', 'claimed', 'org/repo', 'Address review comments on PR 7', 5400, { mission_id: 'M-302' }),
    r('h-5', 'queued', 'org/repo', 'Re-run verification on bender', 86400, { attention: true, stalled: true, attention_reason: 'queued for over 24 h', completed: 0 }),
    r('h-6', 'rejected', 'herdr-web', 'Rewrite the diagnostics timeline in Rust', 7200, { attention: true, attention_reason: 'rejected by target', mission_id: '', completed: 0, remaining: 0 }),
    r('h-7', 'completed', 'herdr-web', 'Add a vision phase to the screenshot reviewer', 18000,
      { origin: 'lineage', cause: 'context_limit', from_model: 'claude-opus', to_model: 'qwen3-vl', model_change: 'switched', checkpoint: 'cp-17', vision: { phase: 'back', outcome: 'ok', request_id: 'vr-9' }, remaining: 0, completed: 3 }),
    r('h-8', 'expired', 'herdr-web', 'Triage the stale attention items', 172800, { attention: true, attention_reason: 'expired before it was claimed', mission_id: '', completed: 0 }),
    r('h-9', 'accepted', 'metabolomics', 'Summarise the batch 14 QC failures', 1200, { mission_id: 'M-303', origin: 'lineage', cause: 'manual', from_model: 'qwen3-coder', to_model: 'qwen3-coder', model_change: 'same', checkpoint: '' }),
  ];
}

function handoffs(path, params, ctx) {
  const rows = handoffRows(ctx);
  if (path === '/api/v1/handoffs/overview') {
    const by = {};
    for (const x of rows) { (by[x.workspace] ||= []).push(x); }
    const count = list => list.reduce((m, x) => ({ ...m, [x.state]: (m[x.state] || 0) + 1 }), {});
    const att = rows.filter(x => x.attention);
    return {
      total: rows.length, states: count(rows), attention_total: att.length,
      workspaces: Object.entries(by).map(([workspace, list]) => ({ workspace, total: list.length, states: count(list), attention: list.filter(x => x.attention).length,
        stalled: list.filter(x => x.stalled).length, latest_at: list[0].updated_at })),
      workspaces_omitted: 0, attention: att, untrusted_text: true, sources: { registry: 'ok', lineage: 'ok' },
    };
  }
  if (path === '/api/v1/handoffs/tree') {
    return { nodes: rows.map(x => ({ id: x.handoff_id, agent: x.target.agent_id, state: x.state, workspace: x.workspace })),
      edges: [{ from: 'h-3', to: 'h-1' }, { from: 'h-1', to: 'h-4' }, { from: 'h-2', to: 'h-5' }], untrusted_text: true };
  }
  if (path === '/api/v1/handoffs') {
    const ws = params.get('workspace');
    const list = rows.filter(x => !ws || x.workspace === ws);
    return { handoffs: list, next_cursor: null, untrusted_text: true };
  }
  const m = /^\/api\/v1\/handoffs\/([^/]+)(\/chain)?$/.exec(path);
  if (!m) return undefined;
  const idx = rows.findIndex(x => x.handoff_id === m[1]);
  if (idx < 0) return undefined;
  if (m[2]) return { handoff_id: m[1], position: idx, more_before: false, more_after: false, chain: rows.slice(0, 4) };
  const x = rows[idx];
  return { ...x, decisions: ['Keep the retry budget per mission, not per request.', 'Surface budget exhaustion as a failed event.'],
    risks: ['The 429 handling in transport.py still counts a retry twice.'], validation: ['pytest tests/test_gateway.py -q'],
    completed: ['Read the gateway contract', 'Drafted the budget type'], remaining: ['Wire the budget into the supervisor'],
    provenance: { harness: 'claude', model: 'claude-opus' }, artifact_refs: ['art-1'],
    history: [{ state: 'queued', actor: { id: 'claude:planner' }, at: x.created_at }, { state: x.state, actor: { id: 'pi:implementer' }, at: x.updated_at }],
    created_by: 'claude:planner', room_id: 'room-1' };
}

// ---------------------------------------------------------------- memory
function memories(ctx) {
  const ago = ctx.ago;
  const m = (n, scope, project, kind, status, text, secs, evidence = []) => ({
    memory_id: `mem-${n}`, bank_id: 'bank-main', scope, project, kind, status, text, revision: n % 3 + 1, created_at: ago(secs + 86400), updated_at: ago(secs),
    provenance: { harness_id: n % 2 ? 'claude' : 'pi', host_id: n % 2 ? 'beast' : 'bender', mission_id: 'M-301', session_id: 's-101', pr_ref: n % 3 ? 'org/repo#7' : '',
      commit_sha: SHA[n % 3], evidence_refs: evidence },
  });
  return [
    m(1, 'project', 'org/repo', 'decision', 'verified', 'The gateway retry budget is per mission. A 429 counts against the budget exactly once, in the supervisor, never in the transport layer.', 3600, ['handoff:h-1', 'pr:org/repo#7']),
    m(2, 'project', 'org/repo', 'fact', 'verified', 'Gateway health is served at /api/v1/health and lists capabilities.', 7200),
    m(3, 'project', 'org/repo', 'lesson', 'candidate', LONG, 1800, ['handoff:h-4']),
    m(4, 'global', '', 'preference', 'verified', 'Run the dashboard fixtures with the browser clock pinned to 2026-10-23T10:00:00Z.', 90000),
    m(5, 'project', 'herdr-web', 'fact', 'superseded', 'The overview polls /api/v1/activity every 5 s.', 172800, ['handoff:h-2']),
    m(6, 'project', 'herdr-web', 'decision', 'retracted', 'Use websockets for the attention feed.', 259200),
    m(7, 'project', 'org/repo', 'fact', 'candidate', 'beast is the only host with a GPU large enough for the 35B model.', 600),
  ];
}

function memory(path, params, ctx) {
  if (path === '/api/v1/memory/status') {
    return { healthy: true, contract: 'memory-v1', server_version: '0.9.4', outbox_pending: 2, dead_letter: 1,
      integrations: { rows: [{ host: 'beast', harness: 'claude', state: 'ok' }, { host: 'bender', harness: 'pi', state: 'ok' }, { host: 'zapp', harness: 'codex', state: 'stale' }, { host: 'nibbler', harness: 'pi', state: 'failed' }] } };
  }
  const all = memories(ctx);
  if (path === '/api/v1/memory/search') {
    const q = (params.get('q') || '').toLowerCase();
    const pick = (k, v) => !params.get(k) || v === params.get(k);
    const hits = all.filter(x => pick('scope', x.scope) && pick('project', x.project) && pick('kind', x.kind) && pick('status', x.status)
      && (!q || (x.text + x.project + x.kind).toLowerCase().includes(q) || q === 'gateway' || q === '*'));
    const offset = Number(params.get('offset') || 0);
    return { results: hits.slice(offset, offset + 20), has_more: false, degraded: false, offline: false, note: '', offset };
  }
  const m = /^\/api\/v1\/memory\/(mem-\d+)$/.exec(path);
  if (!m) return undefined;
  const x = all.find(y => y.memory_id === m[1]);
  if (!x) return undefined;
  return { memory: { ...x, revisions: [{ revision: 1, status: 'candidate', at: x.created_at, text: 'First draft: ' + x.text.slice(0, 60) }, { revision: x.revision, status: x.status, at: x.updated_at, text: x.text }] },
    retrieval_history: x.memory_id === 'mem-1' ? { events: [{ at: ctx.ago(1200), harness: 'pi', host: 'bender', query: 'gateway retry', rank: 1 }, { at: ctx.ago(5000), harness: 'claude', host: 'beast', query: 'budget', rank: 2 }] }
      : { state: 'unknown', reason: 'retrieval log not enabled for this bank' } };
}

// ---------------------------------------------------------------- intelligence
function intelligence(path, params, ctx) {
  const ago = ctx.ago;
  if (path === '/api/v1/intelligence/projects') return { items: [{ id: 'proj-1', name: 'org/repo' }, { id: 'proj-2', name: 'herdr-web' }] };
  if (path === '/api/v1/intelligence/investigations') return { items: [{ id: 'inv-1', name: 'Flaky overview poller' }, { id: 'inv-2', name: 'Gateway retry budget' }] };
  const ev = (id, title, category, label, secs, extra = {}) => ({ event_id: id, title, category, actor: 'pi:implementer', occurred_at: ago(secs), observed_at: ago(secs - 30), label,
    late_arrival: false, superseded: false, tombstoned: false, involved_entities: ['pr:org/repo#7', 'mission:M-301'],
    evidence_refs: [{ source_ref: 'git:org/repo', anchor_type: 'commit', anchor_value: SHA[0].slice(0, 12), git_sha: SHA[0], content_hash: 'sha256:9f2c41' }], ...extra });
  if (/\/projects\/[^/]+\/timeline$/.test(path)) {
    const cat = params.get('category'), src = params.get('source');
    const items = [
      ev('evt-1', 'PR 7 opened with the retry budget change', 'pull_request', 'verified', 7200),
      ev('evt-2', 'CI run 411 passed', 'ci', 'verified', 6800, { involved_entities: ['ci:org/repo#411', 'pr:org/repo#7'] }),
      ev('evt-3', 'Reviewer requested the retry policy move to the supervisor', 'review', 'unverified', 5400, { evidence_refs: [] }),
      ev('evt-4', 'Session s-102 failed the verify stage', 'session', 'verified', 1800, { late_arrival: true, involved_entities: ['session:s-102', 'mission:M-302'] }),
      ev('evt-5', 'Earlier plan for a transport-level budget', 'decision', 'verified', 90000, { superseded: true }),
      ev('evt-6', 'Verification report published', 'artifact', 'verified', 900, { involved_entities: ['artifact:art-1'] }),
    ].filter(x => (!cat || x.category === cat) && !src);
    return { items, total: items.length };
  }
  if (path === '/api/v1/intelligence/proposals') {
    return { items: [
      { id: 'prop-1', action: 'request_ci_repair', payload: { run: 412, repo: 'org/repo' }, requester: 'pi:supervisor', expires_at: ago(-3600), payload_sha256: 'e3b0c44298fc1c14', resource_version: 4 },
      { id: 'prop-2', action: 'link_entities', payload: { from: 'pr:org/repo#7', to: 'mission:M-301' }, requester: 'claude:planner', expires_at: ago(-86400), payload_sha256: '9f86d081884c7d65', resource_version: 1 },
      { id: 'prop-3', action: 'pause_ingestion', payload: {}, requester: 'operator', expires_at: ago(-600), payload_sha256: '2c26b46b68ffc68f', resource_version: 2 },
    ] };
  }
  if (path === '/api/v1/intelligence/relationships') {
    return { items: [
      { from: 'pr:org/repo#7', to: 'mission:M-301', predicate: 'implements', review_state: 'accepted', derivation: 'commit trailer', confidence: 0.95, evidence_refs: [] },
      { from: 'ci:org/repo#412', to: 'pr:org/repo#7', predicate: 'validates', review_state: 'candidate', derivation: 'branch match', confidence: 0.6, evidence_refs: [] },
    ] };
  }
  if (path === '/api/v1/intelligence/findings') {
    return { items: [
      { statement: 'The retry budget change is covered by 3 tests.', verified: true, evidence_refs: [{ source_ref: 'git:org/repo', anchor_type: 'commit', anchor_value: SHA[0].slice(0, 12), git_sha: SHA[0], content_hash: 'sha256:9f2c41' }] },
      { statement: 'Flakiness in the overview poller predates this PR.', verified: false, evidence_refs: [] },
    ] };
  }
  return undefined;
}

// ---------------------------------------------------------------- related
function related(params, ctx) {
  const kind = params.get('kind'), id = params.get('id');
  return {
    kind, id,
    relations: {
      mission: [{ id: 'M-301', label: 'Add retry budget to the gateway client', state: 'running' }],
      agents: [{ id: 's-101', label: 'implementer on beast', state: 'active' }, { id: 's-102', label: 'verifier on bender', state: 'failed' }],
      host: [{ id: 'beast', label: 'beast', state: 'ok' }],
      service: [{ id: 'svc-1', label: 'herdr-gateway', state: 'up' }],
      ci: [{ id: 'ci-411', run_number: 411, label: 'run 411', state: 'success', repo_filter: 'org/repo' }],
      review: [{ id: 'rev-7', label: 'review of PR 7', state: 'changes requested' }],
      pr: [{ repository: 'org/repo', number: 7, label: 'org/repo#7', state: 'open' }],
      llm: { requests: 128, tokens_out: 42113, problems: 2 },
    },
    more: { agents: 2 }, partial: [],
  };
}

// ---------------------------------------------------------------- architecture
const SNAP = 'snap-3f9a1c07';
function snapshots(ctx) {
  const mk = (id, repo, sha, secs, completeness, nodes, edges, host) => ({ snapshot_id: id, repository_id: repo, commit_sha: sha, completeness, counts: { nodes, edges },
    host_id: host, analyzer_version: '0.8.2', worktree_id: `wt${id.slice(-4)}ab`, created_at: ctx.ago(secs) });
  return [
    mk(SNAP, 'github.com/org/repo', SHA[0], 3000, 'complete', 38, 61, 'beast'),
    mk('snap-9b2e4d61', 'github.com/org/repo', SHA[1], 90000, 'partial', 31, 44, 'bender'),
    mk('snap-c4d8e2f6', 'github.com/org/lib', SHA[2], 7200, 'complete', 12, 15, 'beast'),
  ];
}
const N = (id, kind, qn, path, line, kids = 0) => ({ node_id: id, kind, qualified_name: qn, relative_path: path, line_start: line, child_count: kids });
const NODES = {
  'n-repo': N('n-repo', 'repo', 'org/repo', '', 0, 4),
  'n-app': N('n-app', 'package', 'app', 'app', 0, 5),
  'n-web': N('n-web', 'package', 'web', 'web', 0, 2),
  'n-tests': N('n-tests', 'package', 'tests', 'tests', 0, 0),
  'n-docs': N('n-docs', 'package', 'docs', 'docs', 0, 0),
  'n-core': N('n-core', 'module', 'app.core', 'app/core.py', 1, 2),
  'n-gateway': N('n-gateway', 'module', 'app.gateway', 'app/gateway.py', 1, 0),
  'n-super': N('n-super', 'module', 'app.supervisor', 'app/supervisor.py', 1, 0),
  'n-util': N('n-util', 'module', 'app.util', 'app/util.py', 1, 0),
  'n-budget': N('n-budget', 'module', 'app.retry_budget', 'app/retry_budget.py', 1, 0),
  'n-engine': N('n-engine', 'class', 'app.core.Engine', 'app/core.py', 6, 0),
  'n-run': N('n-run', 'function', 'app.core.run', 'app/core.py', 9, 0),
  'n-ui': N('n-ui', 'module', 'web.ui', 'web/ui.ts', 1, 0),
  'n-overview': N('n-overview', 'module', 'web.overview_view', 'web/overview-view.js', 1, 0),
};
const CHILDREN = { '': ['n-app', 'n-web', 'n-tests', 'n-docs'], 'n-repo': ['n-app', 'n-web', 'n-tests', 'n-docs'], 'n-app': ['n-core', 'n-gateway', 'n-super', 'n-util', 'n-budget'],
  'n-web': ['n-ui', 'n-overview'], 'n-core': ['n-engine', 'n-run'] };
const EDGES = [['n-core', 'n-util', 'imports', 3, false], ['n-gateway', 'n-util', 'imports', 2, false], ['n-super', 'n-gateway', 'imports', 4, false], ['n-super', 'n-budget', 'imports', 1, false],
  ['n-gateway', 'n-budget', 'calls', 2, true], ['n-core', 'n-super', 'calls', 5, false], ['n-ui', 'n-overview', 'imports', 1, false]];
const edgeRows = (ids) => EDGES.filter(([a, b]) => ids.includes(a) && ids.includes(b)).map(([a, b, kind, count, inferred]) => ({ from_id: a, to_id: b, kind, count, inferred }));
const ancestors = (id) => (id === 'n-engine' || id === 'n-run' ? ['n-repo', 'n-app', 'n-core'] : id === 'n-ui' || id === 'n-overview' ? ['n-repo', 'n-web'] : ['n-repo', 'n-app']).filter(a => a !== id).map(a => NODES[a]);

function architecture(path, method, params, ctx) {
  const rest = path.replace('/api/v1/architecture/', '');
  if (rest === 'snapshots') return { snapshots: snapshots(ctx) };
  if (rest === 'analyses') {
    return { analyses: [
      { analysis_id: 'an-1', state: 'completed', repository_id: 'github.com/org/repo', rev: SHA[0].slice(0, 8), host_id: 'beast', message: '', progress: 1 },
      { analysis_id: 'an-2', state: 'failed', repository_id: 'github.com/org/repo', rev: 'HEAD', host_id: 'nibbler', message: 'analysis exceeded the 300s wall-clock limit', progress: 0.4 },
      { analysis_id: 'an-3', state: 'progress', repository_id: 'github.com/org/lib', rev: 'main', host_id: 'bender', message: '', progress: 0.62 },
      { analysis_id: 'an-4', state: 'requested', repository_id: 'github.com/org/repo', rev: 'origin/main', host_id: 'zapp', message: '', progress: 0 },
    ] };
  }
  if (rest === 'timeline') {
    return { note: 'Snapshots and agent spawns, matched by exact repository and commit only.',
      architecture: [{ snapshot_id: SNAP, commit_sha: SHA[0], at: ctx.ago(3000), completeness: 'complete', session_ids: ['s-101'] },
        { snapshot_id: 'snap-9b2e4d61', commit_sha: SHA[1], at: ctx.ago(90000), completeness: 'partial', session_ids: [] }],
      agents: [{ session_id: 's-101', at: ctx.ago(3200), parent_session_id: null, commit_sha: SHA[0], snapshot_ids: [SNAP] },
        { session_id: 's-102', at: ctx.ago(2600), parent_session_id: 's-101', commit_sha: '', snapshot_ids: [] }] };
  }
  if (rest === 'diff') {
    return { base: params.get('base'), head: params.get('head'), summary: { nodes_added: 7, nodes_removed: 0, nodes_modified: 2, edges_added: 17, edges_removed: 0 },
      items: [{ change: 'added', type: 'node', id: 'app.retry_budget' }, { change: 'added', type: 'edge', kind: 'imports', from_id: 'app.supervisor', to_id: 'app.retry_budget' },
        { change: 'modified', type: 'node', id: 'app.gateway' }], warnings: ['The older snapshot is partial, so removals may be missing.'] };
  }
  if (rest === 'cross-repo') return { verified: [], inferred: [], ambiguous: [], incomplete: false, warnings: [] };
  const m = /^snapshots\/([^/]+)\/(.+)$/.exec(rest);
  if (!m) return undefined;
  const [, sid, tail] = m;
  if (!snapshots(ctx).some(x => x.snapshot_id === sid)) return undefined;
  if (tail === 'children') {
    const parent = params.get('parent') || '';
    const ids = CHILDREN[parent] || [];
    return { nodes: ids.map(i => NODES[i]), edges: edgeRows(ids), total: ids.length, incomplete: false, next_cursor: null, parent: parent ? NODES[parent] : NODES['n-repo'] };
  }
  if (tail === 'nodes') {
    const q = (params.get('q') || '').toLowerCase();
    const hits = Object.values(NODES).filter(n => n.kind !== 'repo' && (n.qualified_name + n.relative_path).toLowerCase().includes(q));
    return { nodes: hits, edges: [], total: hits.length, incomplete: false, next_cursor: null, parent: null };
  }
  if (tail === 'overlays') {
    return { ci: [{ run_number: 411, state: 'success', branch: 'feat/retry-budget', url: 'https://github.com/org/repo/actions/runs/411' }, { run_number: 412, state: 'failed', branch: 'main', url: '' }],
      pull_requests: [{ pr_ref: 'org/repo#7', state: 'open', url: 'https://github.com/org/repo/pull/7' }],
      sessions: [{ session_id: 's-101', agent_role: 'implementer', flow: 'authored', parent_session_id: '' }, { session_id: 's-102', agent_role: 'verifier', flow: 'reviewed', parent_session_id: 's-101' }],
      worktree_sessions: [{ session_id: 's-103', agent_role: 'reviewer', commit_sha: SHA[1] }, { session_id: 's-104', agent_role: '', commit_sha: '' }],
      missions: [{ mission_key: 'M-301' }], issues: [{ issue_ref: 'org/repo#31', evidence: 'mentioned in the mission goal' }] };
  }
  if (tail === 'base') return sid === SNAP ? { base: { snapshot_id: 'snap-9b2e4d61', commit_sha: SHA[1], base_rev: 'origin/main' } } : { reason: 'no analysis asked for a PR base' };
  if (tail === 'correlations') {
    return { correlations: [{ session_id: 's-101', flow: 'authored', change: 'modified', parent_worker_id: '', pr_ref: 'org/repo#7' }, { session_id: 's-102', flow: 'reviewed', change: 'unchanged', parent_worker_id: 's-101', pr_ref: '' }], notes: [], change: 'modified' };
  }
  const nm = /^nodes\/([^/]+)(\/edges|\/rooms)?$/.exec(tail);
  if (!nm || !NODES[nm[1]]) return undefined;
  const n = { ...NODES[nm[1]], extractor: 'python-ast', confidence: 1 };
  if (nm[2] === '/rooms') return { count: 2 };
  if (nm[2] === '/edges') {
    const mine = EDGES.filter(([a, b]) => a === n.node_id || b === n.node_id);
    return { edges: mine.map(([a, b, kind, , inferred], i) => ({ edge_id: `e_${n.node_id}_${i}`, kind, from_id: a, to_id: b, source_span: { path: NODES[a].relative_path, line_start: 3 + i }, inferred, extractor: inferred ? 'name-match' : 'python-ast' })), incomplete: false };
  }
  return { node: n, ancestors: ancestors(n.node_id) };
}

// ---------------------------------------------------------------- collab (operator mode: the dashboard owner, no sign-in)
function collab(path, ctx) {
  const rest = path.replace('/api/v1/collab', '');
  if (rest === '/config') return { client_id: 'fixture-client', hosted_ui_domain: 'auth.example.test', public_url: 'https://collab.example.test', operator: { user_id: 'operator', email: 'owner@lab.example', status: 'approved', is_operator: true } };
  if (rest === '/rooms') {
    return { rooms: [{ room_id: 'room-1', name: 'Gateway retry budget', role: 'owner' }, { room_id: 'room-2', name: 'Metabolomics review', role: 'contributor' }, { room_id: 'room-3', name: 'Vendor QC watch', role: 'viewer' }] };
  }
  if (rest.startsWith('/admin/users')) return { users: [] };
  const now = Math.floor(ctx.now / 1000);
  if (rest === '/fleet/egress') {
    return { rooms_with_hosts: ['room-1'], requests: [
      { id: 'eg-1', room_id: 'room-1', host_port: 'pypi.org:443', agent_id: 'agent-impl', state: 'open', internal: false },
      { id: 'eg-2', room_id: 'room-2', host_port: '10.0.4.17:5432', agent_id: 'agent-qc', state: 'open', internal: true },
      { id: 'eg-3', room_id: 'room-1', host_port: 'files.pythonhosted.org:443', agent_id: 'agent-impl', state: 'approved', expires_at: now + 86400 },
    ] };
  }
  if (rest === '/fleet/agent-links') return { agents: [] };
  if (rest === '/fleet/helpers') {
    return { paused: false, now, helpers: [
      { helper_id: 'hlp-1', role: 'reviewer', task: 'Review the retry budget change for 429 double counting', status: 'running', requester_agent_id: 'agent-impl', room_id: 'room-1', created_epoch: now - 1200, expires_epoch: now + 2400 },
      { helper_id: 'hlp-2', role: 'summariser', task: '', status: 'done', requester_agent_id: 'agent-qc', room_id: 'room-2', created_epoch: now - 20000, expires_epoch: now - 10000 },
    ] };
  }
  if (rest === '/fleet/summary-fix/helpers') return { configured: false, helpers: [] };
  const eg = /^\/fleet\/egress\/([^/]+)$/.exec(rest);
  if (eg) return { allowlist: ['api.github.com:443', 'pypi.org:443'], entries: [{ host_port: 'api.github.com:443', kind: 'permanent' }, { host_port: 'files.pythonhosted.org:443', kind: 'approved' }, { host_port: 'llm.beast:8011', kind: 'model' }] };
  return undefined;
}

export function answer(pathname, method, params, ctx) {
  if (pathname === '/api/v1/attention') return attention(params, ctx);
  if (/^\/api\/v1\/attention\/(ack|snooze|clear)$/.test(pathname)) return { stored: true };
  if (pathname === '/api/v1/fabric/names') return { available: true, names: ['local.metabolomics.us', 'beast.metabolomics.us', 'bender.metabolomics.us'] };
  if (pathname === '/api/v1/activity') return { events: envelopes(ctx).slice(0, Number(params.get('limit') || 50)) };
  if (pathname === '/api/v1/events') return eventsFor(params, ctx);
  if (pathname === '/api/v1/related') return related(params, ctx);
  if (pathname.startsWith('/api/v1/handoffs')) return handoffs(pathname, params, ctx);
  if (pathname.startsWith('/api/v1/memory/')) return memory(pathname, params, ctx);
  if (pathname.startsWith('/api/v1/intelligence/')) return intelligence(pathname, params, ctx);
  if (pathname.startsWith('/api/v1/architecture/')) return architecture(pathname, method, params, ctx);
  if (pathname.startsWith('/api/v1/collab/')) return collab(pathname, ctx);
  return undefined;
}

// /api/v1 fixtures: agents. See ../dashboard-fixtures.mjs for the contract.
// Sessions, session inspector/summary/evolution, mission detail/story/tests/reviews, assistant sub-resources.

const RUNTIME_PI = { runtime_kind: 'standard-pi', js_engine: 'node', js_engine_version: '22.11.0', fallback_reason: '' };
const RUNTIME_BOLT = { runtime_kind: 'pi-bolt', js_engine: 'bolt', js_engine_version: '0.9.2', fallback_reason: '' };
const RUNTIME_JIT = { runtime_kind: 'pi-bolt-jit', js_engine: 'bolt', js_engine_version: '0.9.2', fallback_reason: 'jit warmup' };

function sessions(ago) {
  const base = (o) => ({ parent_session_id: '', capabilities: { attach_terminal: true }, runtime: RUNTIME_PI, ...o });
  return [
    base({ session_id: 's-101', host_id: 'zapp', cwd: '/srv/work/gateway', display_name: 'Gateway rollout lead', agent_role: 'lead', agent_handle: 'claude',
      status: 'active', stage: 'implementation', summary: 'Rolling the unified gateway scheduling image out to zapp; canary healthy, bender next.',
      branch: 'feat/gateway-scheduling-image', model: 'claude-sonnet-5-5', mission_id: 'M-301', mission_key: 'M-301', pid: 41872,
      cmdline: 'claude --resume --model claude-sonnet-5-5', created_at: ago(7200), updated_at: ago(60), last_seen_at: ago(45) }),
    base({ session_id: 's-102', host_id: 'bender', cwd: '/srv/work/gateway', display_name: 'Bender image builder', agent_role: 'worker', agent_handle: 'pi',
      status: 'working', stage: 'implementation', summary: 'Building the scheduling image on bender.',
      branch: 'feat/gateway-scheduling-image', model: 'qwen3.6-35b-a3b', mission_id: 'M-301', mission_key: 'M-301', pid: 20931,
      cmdline: 'pi --model qwen3.6-35b-a3b', created_at: ago(5400), updated_at: ago(130), last_seen_at: ago(120), parent_session_id: 's-101', runtime: RUNTIME_BOLT }),
    base({ session_id: 's-103', host_id: 'beast', cwd: '/srv/work/router', display_name: 'Router test migrator', agent_role: 'worker', agent_handle: 'pi',
      status: 'running', stage: 'verification', summary: 'Migrating router tests to the new fixtures. Last report is stale.',
      branch: 'chore/router-fixtures', model: 'qwen3.6-35b-a3b', mission_id: '', mission_key: 'pi:M-202', pid: 7714,
      cmdline: 'pi --model qwen3.6-35b-a3b', created_at: ago(20000), updated_at: ago(10800), last_seen_at: ago(10800), runtime: RUNTIME_JIT }),
    base({ session_id: 's-104', host_id: 'nibbler', cwd: '/srv/work/ci-cache', display_name: 'CI cache replacer', agent_role: 'worker', agent_handle: 'claude',
      status: 'failed', stage: 'verification', summary: 'CI cache layer swap failed verification: restore step exits 137.',
      branch: 'fix/ci-cache-layer', model: 'claude-sonnet-5-5', mission_id: 'M-302', mission_key: 'M-302', pid: 0,
      cmdline: 'claude --model claude-sonnet-5-5', created_at: ago(90000), updated_at: ago(86400), last_seen_at: ago(86400) }),
    base({ session_id: 's-105', host_id: 'beast', cwd: '/srv/work/bench', display_name: 'Benchmark publisher', agent_role: 'worker', agent_handle: 'pi',
      status: 'completed', stage: 'done', summary: '', branch: 'main', model: 'qwen3.6-35b-a3b', mission_id: 'M-303', mission_key: 'M-303', pid: 0,
      cmdline: 'pi', created_at: ago(180000), updated_at: ago(172800), last_seen_at: ago(172800) }),
    base({ session_id: 's-106', host_id: 'nibbler', cwd: '/srv/work/telemetry', display_name: 'Telemetry label tidier', agent_role: 'worker', agent_handle: 'claude',
      status: 'closed', stage: 'done', summary: 'Renamed host telemetry labels; merged.', branch: 'chore/telemetry-labels', model: 'claude-haiku-4-5',
      mission_id: 'M-304', mission_key: 'M-304', pid: 0, cmdline: 'claude', created_at: ago(270000), updated_at: ago(259200), last_seen_at: ago(259200) }),
    base({ session_id: 's-107', host_id: 'beast', cwd: '/srv/work/gateway', display_name: 'Health endpoint author', agent_role: 'worker', agent_handle: 'pi',
      status: 'active', stage: 'implementation', summary: 'Adding the /healthz endpoint to the gateway.', branch: 'feat/healthz', model: 'qwen3.6-35b-a3b',
      mission_id: '', mission_key: 'pi:M-201', pid: 5590, cmdline: 'pi', created_at: ago(3000), updated_at: ago(90), last_seen_at: ago(80), runtime: RUNTIME_BOLT }),
  ];
}

function missionRecord(key, ago) {
  const sess = sessions(ago);
  const m = {
    'M-301': { source: 'herdr', title: 'Roll out the unified gateway scheduling image', status: 'active', stage: 'implementation', host_id: 'zapp', cwd: '/srv/work/gateway',
      purpose: 'Roll the gateway image out to zapp and bender', created_at: ago(14400), updated_at: ago(7200), telemetry_state: 'stale', report_age_seconds: 7200, mission_key: 'M-301' },
    'M-302': { source: 'herdr', title: 'Replace the CI cache layer', status: 'failed', stage: 'verification', host_id: 'bender', cwd: '/srv/work/ci-cache',
      purpose: '', created_at: ago(95000), updated_at: ago(86400), telemetry_state: 'stale', report_age_seconds: 86400, mission_key: 'M-302' },
    'M-303': { source: 'herdr', title: 'Publish the benchmark workspace', status: 'complete', stage: 'done', host_id: 'beast', cwd: '/srv/work/bench',
      purpose: 'Publish the benchmark workspace', created_at: ago(180000), updated_at: ago(172800), telemetry_state: 'stale', report_age_seconds: 172800, mission_key: 'M-303' },
    'M-304': { source: 'herdr', title: 'Tidy the host telemetry labels', status: 'closed', stage: 'done', host_id: 'nibbler', cwd: '/srv/work/telemetry',
      purpose: 'Tidy labels', created_at: ago(270000), updated_at: ago(259200), telemetry_state: 'stale', report_age_seconds: 259200, mission_key: 'M-304' },
    'pi:M-201': { source: 'pi', title: 'Add a health endpoint to the gateway', status: 'active', stage: 'implementation', host_id: 'beast', cwd: '/srv/work/gateway',
      purpose: 'Expose /healthz with dependency checks', created_at: ago(3600), updated_at: ago(90), telemetry_state: 'live', report_age_seconds: 90, mission_key: 'pi:M-201' },
    'pi:M-202': { source: 'pi', title: 'Migrate the router tests to the new fixtures', status: 'active', stage: 'verification', host_id: 'bender', cwd: '/srv/work/router',
      purpose: '', created_at: ago(20000), updated_at: ago(600), telemetry_state: 'live', report_age_seconds: 600, mission_key: 'pi:M-202' },
    'pi:M-203': { source: 'pi', title: 'Fix the flaky relay sticky-bias test', status: 'blocked', stage: 'review', host_id: 'zapp', cwd: '/srv/work/relay',
      purpose: 'Stabilise the sticky-bias test', created_at: ago(30000), updated_at: ago(1200), telemetry_state: 'live', report_age_seconds: 1200, mission_key: 'pi:M-203' },
    'pi:M-204': { source: 'pi', title: 'Draft the quarterly capacity note', status: 'active', stage: 'planning', host_id: 'nibbler', cwd: '/srv/work/capacity',
      purpose: 'Capacity note for Q4', created_at: ago(40000), updated_at: ago(14400), telemetry_state: 'stale', report_age_seconds: 14400, mission_key: 'pi:M-204' },
  }[key];
  if (!m) return undefined;
  return { mission_id: key, ...m, sessions: sess.filter((s) => s.mission_key === key || s.mission_id === key) };
}

function inspector(id, view, ago) {
  const ev = (seq, kind, ago_s, data, extra = {}) => ({ seq, rev: 1, kind, ts: ago(ago_s), agent_id: 'a-1', data, truncated: false, redacted: false, sensitive: false, ...extra });
  const events = [
    ev(1, 'lifecycle', 3000, { phase: 'started' }),
    ev(2, 'message', 2950, { role: 'user', text: 'Roll the gateway scheduling image out to zapp, then bender. Keep the canary at 10% until the health checks are green.', withheld: false }),
    ev(3, 'reasoning', 2900, { text: 'Plan: build image, push, canary on zapp, verify, promote.' }),
    ev(4, 'tool', 2880, { tool: 'bash', args_preview: 'docker build -t gateway:sched .', status: 'ok', exit_code: 0, duration_ms: 48210, output: 'Successfully built 3f9c2a1b7d10\nSuccessfully tagged gateway:sched', output_more: false }),
    ev(5, 'file', 2800, { op: 'edit', path: 'deploy/gateway.yaml', additions: 12, deletions: 3, diff: '@@ -10,3 +10,12 @@\n-  image: gateway:1.4\n+  image: gateway:sched\n+  canary: 10', diff_more: false }),
    ev(6, 'tool', 2700, { tool: 'bash', args_preview: 'kubectl rollout status deploy/gateway', status: 'failed', exit_code: 1, duration_ms: 120000, output: 'error: timed out waiting for the condition', output_more: true }),
    ev(7, 'test', 2500, { command: 'go test ./gateway/...', run_id: 'run-77', status: 'failed', passed: 41, failed: 2, skipped: 1,
      failures: [{ name: 'TestSchedulerStickyBias', file: 'gateway/sched_test.go', line: 212, message: 'expected node bender, got zapp' }, { name: 'TestCanaryPromote', file: 'gateway/canary_test.go', line: 88, message: '' }] }),
    ev(8, 'usage', 2400, { input_tokens: 84210, output_tokens: 6320, context_tokens: 91000, model: 'claude-sonnet-5-5' }),
    ev(9, 'message', 2300, { role: 'assistant', text: 'The canary on zapp is healthy. Two scheduler tests fail on sticky bias; I am fixing them before promoting to bender.', withheld: false }),
    ev(10, 'model', 1800, { previous_model: 'claude-sonnet-5-5', model: 'claude-opus-4-7', reason: 'escalation after repeated test failure' }),
    ev(11, 'message', 120, { role: 'assistant', text: 'Fixed sticky bias; rerunning the suite.', withheld: false }),
  ];
  const shown = view === 'conversation' ? events.filter((e) => e.kind === 'message') : events;
  return {
    epoch: 'ep-1', events: shown, has_more: false, oldest_available_seq: 1, telemetry: 'live', dropped: 0,
    capabilities: { declared: true, reasoning: 'none', streaming_tools: true, diffs: true },
    summary: {
      ended: false, last_event_age_s: 120, running_tools: [{ tool: 'bash', args_preview: 'go test ./gateway/...' }],
      tests: [{ status: 'failed' }, { status: 'passed' }],
      usage: { input_tokens: 84210, output_tokens: 6320, reasoning_tokens: 1200, cache_read_tokens: 51000, context_pct: 46, latest: { context_window: 200000 } },
      client_dropped: 0,
      files: [{ path: 'deploy/gateway.yaml', ops: ['edit'], additions: 12, deletions: 3, agents: ['a-1'], last_seq: 5, sensitive: false },
        { path: 'gateway/sched.go', ops: ['edit', 'create'], additions: 40, deletions: 9, agents: ['a-1'], last_seq: 11, sensitive: false },
        { path: '.env.production', ops: ['read'], additions: 0, deletions: 0, agents: ['a-1'], last_seq: 4, sensitive: true }],
      agents: [{ agent_id: 'a-1', display_name: 'Gateway rollout lead', parent_agent_id: '', agent_handle: 'claude', agent_role: 'lead' },
        { agent_id: 'a-2', display_name: 'Bender image builder', parent_agent_id: 'a-1', agent_handle: 'pi', agent_role: 'worker' }],
      association: { mission_id: 'M-301', branch: 'feat/gateway-scheduling-image', worktree: '/srv/work/gateway',
        pr: { repo: 'org/repo', number: 7, url: 'https://github.com/org/repo/pull/7' }, handoff_id: 'h-1', handoff_from: 's-102', parent_session_id: '' },
      models: [{ model: 'claude-opus-4-7', previous_model: 'claude-sonnet-5-5', reason: 'escalation after repeated test failure' }],
    },
  };
}

function summary(ago) {
  return {
    state: 'ready', overview: 'The agent is rolling the unified gateway scheduling image out to zapp and bender, with a 10% canary gate.',
    where_left_off: 'Rerunning the gateway suite after fixing sticky bias; canary still pinned at 10%.',
    discussed: ['Canary strategy', 'Scheduler sticky bias', 'Image tagging'],
    decisions: ['Keep canary at 10% until health checks are green', 'Escalate to a larger model after repeated failures'],
    completed: ['Built gateway:sched', 'Canary on zapp healthy'],
    next_steps: ['Promote to bender', 'Update the runbook'],
    blockers: ['TestCanaryPromote still fails intermittently'],
    coverage: { status: 'partial', message_count: 11, reasons: ['Earlier history was compacted'] },
    generated_at: ago(300), source_message_count: 11, model: 'qwen3.6-35b-a3b',
    full_report: { state: 'ready', report: 'Full report: canary healthy on zapp; two scheduler tests failed and were fixed.' },
  };
}

function evolution(id, ago) {
  const rec = (value, reason = '') => ({ value, reason });
  return {
    session_id: id,
    sources: { sessions: 'ok', handoffs: 'ok', events: 'ok', github: 'ok', reviews: 'ok', usage: 'unknown' },
    phases: [
      { key: 'assigned', title: 'Assigned', state: 'recorded', reason: '', omitted: 0, items: [
        { kind: 'session_record', at: ago(7200), name: 'Gateway rollout lead', role: 'lead', runtime: 'standard-pi', model: 'claude-sonnet-5-5', host_id: 'zapp', mission_id: 'M-301', repository: 'org/repo', branch: 'feat/gateway-scheduling-image', rule: 'session_record' },
        { kind: 'handoff_in', at: ago(7000), from: { harness: 'pi', agent_id: 'a-2', session_id: 's-102' }, to: { harness: 'claude', agent_id: 'a-1', session_id: 's-101' }, state: 'accepted',
          goal: 'Take over the rollout', completed: ['image built'], remaining: ['canary', 'promote'], decisions: ['10% canary'], time_to_accept_s: 42, problem: '', rule: 'session_record' }] },
      { key: 'worked', title: 'Worked', state: 'recorded', reason: '', omitted: 0, items: [
        { kind: 'run', started_at: ago(6800), calls: 38, failed: 4, running: false, tools: [{ tool: 'bash', calls: 22 }, { tool: 'edit', calls: 12 }], idle_before_s: 0, tools_omitted: 0 },
        { kind: 'status', at: ago(6000), from: 'active', to: 'failed', restart: false },
        { kind: 'status', at: ago(5900), from: 'failed', to: 'active', restart: true }] },
      { key: 'handed_off', title: 'Handed off', state: 'none', reason: 'No handoff out is recorded for this agent.', omitted: 0, items: [] },
      { key: 'produced', title: 'Produced', state: 'recorded', reason: '', omitted: 0, items: [
        { kind: 'commit', at: ago(5000), sha: '3f9c2a1b7d10', run_number: 412, ci_url: 'https://ci.example/runs/412', branch: 'feat/gateway-scheduling-image' },
        { kind: 'pull_request', at: ago(4800), number: 7, status: 'open', url: 'https://github.com/org/repo/pull/7', trail: '#/pr/org/repo/7' },
        { kind: 'ci_run', at: ago(4700), run_number: 412, ci_url: 'https://ci.example/runs/412', state: 'success' }] },
      { key: 'verified', title: 'Verified', state: 'recorded', reason: '', omitted: 0, items: [
        { kind: 'review', at: ago(4000), review_kind: 'code', verdict: 'changes_requested', reviewer: 'codex', changes_requested: 2 },
        { kind: 'pull_request_outcome', ref: 'org/repo#7', status: 'open', caveat: 'Signals cover every agent on the pull request', signals: ['ci success', '2 changes requested'] }] },
      { key: 'outcome', title: 'Outcome', state: 'recorded', reason: '', omitted: 0, items: [
        { kind: 'status', at: ago(60), value: 'active', last_reported_status: 'active', stale: false }] },
      { key: 'cost', title: 'Cost', state: 'unknown', reason: 'No usage record is linked to this agent.', omitted: 0, items: [] },
    ],
    learning_record: {
      outcome: rec('active'), duration_s: rec(7200), tool_calls: rec({ total: 38, failed: 4, retried: 2 }), handoffs_in: rec(1),
      handoffs_out: rec({ total: 0, rejected: 0 }), prs: rec(1), cost_usd_estimate: rec(null, 'No usage record is linked to this agent.'),
    },
    hints: { mission_id: 'M-301', same_mission_sessions: [{ session_id: 's-102', name: 'Bender image builder' }], note: 'Agents that share this mission are a hint only; they feed no number.' },
    derived: { retries: 'A call whose agent, tool and arguments equal an earlier call that ended in an error.', restarts: 'A move from a terminal status to a non-terminal one.' },
    restarts: { value: 1, reason: '' },
  };
}

function story(key, ago) {
  const m = missionRecord(key, ago);
  const failed = key === 'M-302';
  return {
    header: { result: failed ? 'failed' : key === 'M-301' || key.startsWith('pi:') ? 'running' : 'succeeded', duration_s: 7200, agent_count: 3, tokens_out: 48210, repository: 'org/repo',
      branch: 'feat/gateway-scheduling-image', hosts: ['zapp', 'bender'], summary: { pr: { number: 7, state: 'open' }, ci: failed ? 'failed' : 'success', tests: { passed: 41, failed: failed ? 3 : 0 } },
      inferred: false, recorded_before_outcomes: false },
    objective: m.purpose || m.title,
    needs: failed ? [{ title: 'Review the restore failure', detail: 'Restore step exits 137 on bender', href: '#/ci' }] : [],
    events: [
      { kind: 'start', ts: ago(14400), text: 'Mission started', span_s: 0 },
      { kind: 'agent', ts: ago(14000), text: 'Gateway rollout lead joined', span_s: 600, ref: { kind: 'session', id: 's-101' } },
      { kind: 'tests', ts: ago(9000), text: '41 passed, 2 failed', span_s: 120 },
      { kind: 'pr', ts: ago(8000), text: 'Opened PR #7', span_s: 0, ref: { kind: 'pr', repository: 'org/repo', number: 7 } },
      { kind: 'ci', ts: ago(7800), text: 'CI run 412 succeeded', span_s: 300 },
      { kind: 'review', ts: ago(7500), text: 'Changes requested', span_s: 0 },
      { kind: 'end', ts: ago(7200), text: failed ? 'Failed verification' : 'Still running', span_s: 0 },
    ],
    more: false,
    agents: [
      { label: 'Gateway rollout lead', role: 'lead', model: 'claude-sonnet-5-5', outcome: 'active', started_at: ago(14000), ended_at: null, summary: 'Rolling out the image.', session_id: 's-101' },
      { label: 'Bender image builder', role: 'worker', model: 'qwen3.6-35b-a3b', outcome: 'done', started_at: ago(12000), ended_at: ago(9000), summary: '', session_id: 's-102' },
    ],
    outcome: { agent_count: 2, tests: { runs: 3, passed: 41, failed: failed ? 3 : 0 }, ci: [{ repository: 'org/repo', branch: 'feat/gateway-scheduling-image', run: 412, state: 'success' }],
      reviews: { approved: 1, changes_requested: 1, open: 0 }, prs: [{ repository: 'org/repo', number: 7, state: 'open' }] },
    usage: { requests: 214, input_tokens: 612000, output_tokens: 48210, lower_bound: true },
    partial: [],
  };
}

function activity(key, ago) {
  const m = missionRecord(key, ago);
  return {
    contract_version: 1, mission_key: key, host_id: m.host_id, session_id: 's-107', instance_id: 'inst-1', observed_at: ago(60), snapshot_at: ago(60), activity_snapshot_at: ago(60),
    connection: 'connected', mission: { id: key.replace('pi:', ''), title: m.title, status: m.status },
    freshness: { state: 'live', age_seconds: 60, stale_after_seconds: 900, disconnect_after_seconds: 3600 }, mission_revision: 14, activity_revision: 88,
    activity: {
      health: 'ok', currentActivity: { summary: 'Writing the /healthz handler and its dependency checks' }, currentObjective: m.title,
      acceptanceCoverage: { completed: 3, total: 5 }, workflowProgress: { basis: 'weighted_dag', completed: 4, total: 7 },
      lastOutputAt: ago(70), lastMeaningfulProgressAt: ago(200), lastHeartbeatAt: ago(30), currentActivityStartedAt: ago(500),
      waitingReason: '', waitingSince: null, nextAction: 'Run the gateway tests', completionStatus: 'in_progress', reason: '', action: '',
      workerDetails: [
        { workerId: 'w-1', taskId: 't-1', state: 'running', health: 'ok', startedAt: ago(900), endedAt: null, currentActivity: 'Editing gateway/health.go', route: 'local', model: 'qwen3.6-35b-a3b', node: 'beast', host: 'beast',
          lastOutputPreview: 'func Healthz(w http.ResponseWriter, r *http.Request) {', inferenceWaitMs: 1800, fallbacks: 0, repeatedErrors: 0, repeatedCycles: 0, waitingReason: '' },
        { workerId: 'w-2', taskId: 't-2', state: 'running', health: 'warn', startedAt: ago(1500), endedAt: null, currentActivity: 'Waiting on inference', route: 'fabric', model: 'qwen3.6-35b-a3b', node: 'bender', host: 'bender',
          lastOutputPreview: '', inferenceWaitMs: 42000, fallbacks: 2, repeatedErrors: 3, repeatedCycles: 1, waitingReason: 'inference queue' },
        { workerId: 'w-3', taskId: 't-3', state: 'done', health: 'ok', startedAt: ago(3000), endedAt: ago(2000), currentActivity: '', route: 'local', model: 'qwen3.6-35b-a3b', node: 'beast', host: 'beast',
          lastOutputPreview: 'ok', inferenceWaitMs: 0, fallbacks: 0, repeatedErrors: 0, repeatedCycles: 0, waitingReason: '' },
      ],
      modelUse: { pin: 'qwen3.6-35b-a3b', agents: [{ role: 'worker', workerId: 'w-1', model: 'qwen3.6-35b-a3b', onPin: true, why: '', state: 'running' }, { role: 'reviewer', workerId: 'w-2', model: 'qwen3-coder-30b', onPin: false, why: 'pin saturated', state: 'running' }] },
      tests: { running: 1, passed: 12, failed: 1, completed: 13, total: 14, failures: ['TestHealthzDegraded'] },
      review: { status: 'open', blockingOpen: 1, findings: [{ severity: 'high', status: 'open', summary: 'Healthz leaks internal hostnames' }, { severity: 'low', status: 'resolved', summary: 'Rename handler' }] },
      changes: { changedFiles: ['gateway/health.go', 'gateway/health_test.go', 'deploy/gateway.yaml'] },
      recovery: [{ attempt: 1, status: 'recovered', summary: 'Worker w-2 restarted after timeout', action: 'restart' }],
      errors: [{ count: 3, example: 'inference timeout after 42s' }],
      activity: [
        { at: ago(70), workerId: 'w-1', type: 'edit', summary: 'Edited gateway/health.go', command: '' },
        { at: ago(200), workerId: 'w-1', type: 'tool', summary: 'Ran tests', command: 'go test ./gateway/...' },
        { at: ago(600), workerId: 'w-2', type: 'error', summary: 'Inference timeout', command: '' },
      ],
    },
  };
}

const failedTest = (ago) => ({ status: 'failed', passed: 38, failed: 3, skipped: 1, total: 42, started_at: ago(9000), ended_at: ago(8900) });

export function answer(pathname, method, params, ctx) {
  const { ago } = ctx;
  const get = method === 'GET';
  if (get && pathname === '/api/v1/sessions') return { sessions: sessions(ago) };
  let m = pathname.match(/^\/api\/v1\/sessions\/([^/]+)(\/.*)?$/);
  if (m && get) {
    const id = decodeURIComponent(m[1]);
    const sub = m[2] || '';
    const rec = sessions(ago).find((s) => s.session_id === id);
    if (!rec) return undefined;
    if (sub === '') return rec;
    if (sub === '/summary') return summary(ago);
    if (sub === '/evolution') return evolution(id, ago);
    if (sub === '/inspector') return inspector(id, params.get('view') || 'all', ago);
    const ev = sub.match(/^\/inspector\/events\/(\d+)$/);
    if (ev) return inspector(id, 'all', ago).events.find((e) => e.seq === Number(ev[1]));
    if (sub === '/inspector/preferences') return { preferences: { view: 'all', follow: true } };
    if (sub === '/commits') return { state: 'linked', repository_id: 'org/repo', omitted: 0, commits: [
      { sha: '3f9c2a1b7d10aa', at: ago(5000), pull_request: 7, run_number: 412, state: 'success', match: 'commit', ci_url: 'https://ci.example/runs/412' },
      { sha: '9be01c44d2f3aa', at: ago(3200), pull_request: 0, run_number: 415, state: 'failed', match: 'branch', ci_url: 'https://ci.example/runs/415' }],
      pull_requests: [{ number: 7, status: 'open', at: ago(4800), url: 'https://github.com/org/repo/pull/7' }] };
    return undefined;
  }
  if (m && method === 'POST' && m[2] === '/terminal') {
    return { commands: [{ form: 'ssh', label: 'SSH', command: 'ssh zapp -t herdr attach s-101' }], recommended: 'ssh', viewer_host: 'zapp', note: '' };
  }
  m = pathname.match(/^\/api\/v1\/missions\/([^/]+)(\/.*)?$/);
  if (m && get) {
    const key = decodeURIComponent(m[1]);
    const sub = m[2] || '';
    const rec = missionRecord(key, ago);
    if (!rec) return undefined;
    if (sub === '') return rec;
    if (sub === '/activity') return key.startsWith('pi:') ? { activity: activity(key, ago) } : undefined;
    if (sub === '/story') return story(key, ago);
    if (sub === '/timeline') return { timeline: [
      { id: 't1', kind: 'stage', status: 'ok', title: 'Entered implementation', ts: ago(14000), ref: null },
      { id: 't2', kind: 'tests', status: 'failed', title: 'Gateway suite: 2 failed', ts: ago(9000), ref: null },
      { id: 't3', kind: 'pr', status: 'ok', title: 'PR #7 opened', ts: ago(8000), ref: { kind: 'pr', repository: 'org/repo', number: 7 } }] };
    if (sub === '/tests') return { tests: [failedTest(ago), { status: 'passed', passed: 42, failed: 0, skipped: 0, total: 42, started_at: ago(3000), ended_at: ago(2900) }, { status: 'running', passed: 10, failed: 0, skipped: 0, total: 0, started_at: ago(100), ended_at: null }] };
    if (sub === '/builds') return { builds: [failedTest(ago)] };
    if (sub === '/reviews') return { reviews: [
      { verdict: 'changes_requested', reviewer: 'codex', completed_at: ago(7500), findings: [
        { severity: 'high', summary: 'Canary promotion skips the health gate', detail: 'promote() proceeds when the health probe times out; treat a timeout as unhealthy.' },
        { severity: 'low', summary: 'Rename the image tag', detail: '' }] },
      { verdict: 'approved', reviewer: 'claude', completed_at: ago(3000), findings: [] }] };
    if (sub === '/pulls') return { pulls: [{ number: 7, status: 'open', url: 'https://github.com/org/repo/pull/7', branch: 'feat/gateway-scheduling-image', base: 'main', repository: 'org/repo' }] };
    return undefined;
  }
  if (get && pathname === '/api/v1/assistant-graph') return { nodes: [
    { id: 'asst_1', kind: 'assistant', name: 'Personal Assistant', type: 'personal', status: 'ready', enabled: true, can_delegate: true, delegate_scopes: ['research'], parent_id: '',
      setup: [{ id: 'identity', state: 'ok', detail: '' }, { id: 'connector', state: 'pending', detail: 'No mailbox connected' }], placement: { state: 'planned', host: 'bender', reasons: ['free GPU'] } },
    { id: 'd-1', kind: 'delegation', name: 'Summarise inbox', status: 'running' },
    { id: 'sp-1', kind: 'specialist', name: 'Researcher', bound: true, role: 'research' },
    { id: 'bender', kind: 'host', name: 'bender', status: 'ok', backend: 'omlx', free: { cpus: 8, memory_mb: 24000, gpus: 1 }, capabilities: ['vision'] }],
    edges: [{ from: 'asst_1', to: 'd-1', kind: 'delegated' }, { from: 'asst_1', to: 'sp-1', kind: 'linked' }, { from: 'asst_1', to: 'bender', kind: 'runs_on' }] };
  if (get && pathname === '/api/v1/assistant-types') return { types: [{ type: 'personal', label: 'Personal' }] };
  if (get && pathname === '/api/v1/assistant-routines') return { routines: [
    { routine_id: 'r-1', assistant_id: 'asst_1', name: 'Morning briefing', trigger_type: 'cron', timezone: 'America/Los_Angeles', next_at: ago(-3600), last_result: 'ok', enabled: true },
    { routine_id: 'r-2', assistant_id: 'asst_1', name: 'Weekly digest of failed missions across every host and repository', trigger_type: 'cron', timezone: 'UTC', next_at: ago(-86400), last_result: 'failed', enabled: false },
    { routine_id: 'r-3', assistant_id: 'asst_1', name: 'On mission failure', trigger_type: 'event', timezone: '', next_at: null, last_result: '', enabled: true }] };
  if (get && pathname === '/api/v1/assistant-delegations') return { delegations: [
    { delegation_id: 'dl-1', assistant_id: 'asst_1', status: 'running', task: 'Summarise the unread inbox and flag anything from the gateway team', artifact_refs: [] },
    { delegation_id: 'dl-2', assistant_id: 'asst_1', status: 'queued', task: 'Draft the capacity note', artifact_refs: ['art-1'] },
    { delegation_id: 'dl-3', assistant_id: 'asst_1', status: 'failed', task: 'Book travel', artifact_refs: [] }] };
  m = pathname.match(/^\/api\/v1\/assistants\/asst_1\/(.+)$/);
  if (m && get) {
    if (m[1] === 'threads/thr_1') return { messages: [
      { role: 'user', content: 'What is blocking the gateway rollout?', provenance: null },
      { role: 'assistant', content: 'Two scheduler tests fail on sticky bias (TestSchedulerStickyBias, TestCanaryPromote). The canary on zapp is healthy, so only the bender promotion is held.', provenance: 'mission M-301' },
      { role: 'user', content: 'Ok, remind me tomorrow.', provenance: null }] };
    if (m[1] === 'permissions') return { tools: [
      { tool_id: 'mail.read', sensitive: false, granted: true }, { tool_id: 'mail.send', sensitive: true, granted: false },
      { tool_id: 'calendar.write', sensitive: true, granted: true }, { tool_id: 'files.read', sensitive: false, granted: true }],
      grants: [{ tool_id: 'calendar.write', scope: 'work calendar', expires_at: ago(-86400), constraints: { preauthorized: true } }, { tool_id: 'mail.read', scope: 'all', expires_at: null, constraints: {} }] };
    if (m[1] === 'memory') {
      const q = (params.get('q') || '').toLowerCase();
      const all = [{ memory_id: 'mem-1', content: 'Prefers terse status updates.' }, { memory_id: 'mem-2', content: 'Gateway rollout owner is the zapp team; escalate to them for canary decisions and any change that touches the scheduling image.' },
        { memory_id: 'mem-3', content: 'Timezone: America/Los_Angeles.' }];
      return { memory: all.filter((x) => x.content.toLowerCase().includes(q)) };
    }
    if (m[1] === 'diagnostics') return { assistant_id: 'asst_1', status: 'ready', model_policy: 'auto', last_run_state: 'ok', last_run_at: ago(900), open_delegations: 2, grants: 2, memory_items: 3, connectors: 0, placement_host: 'bender' };
  }
  return undefined;
}

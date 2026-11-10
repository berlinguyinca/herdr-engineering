// Delivery area fixtures: CI, deployments, reviews, review gate, GitHub, pull-request trail, artifacts, publications, work graph.
// Every timestamp comes from ctx.ago(seconds); nothing here is random or reads the clock.
import {IDS} from './ids.mjs';

const H = 3600, D = 86400;
const PROVIDER = 'https://ci.herdr.example';
const SHA = n => `${n.toString(16).padStart(2, '0')}c0ffee${'a1b2c3d4e5f6'.repeat(4)}`.slice(0, 40);
const epoch = (ctx, seconds) => Math.floor(Date.parse(ctx.ago(seconds)) / 1000);
const page = (params, items, fallback = 20) => {
  const per = Math.max(1, Number(params.get('per_page') || params.get('limit') || fallback));
  const at = Math.max(1, Number(params.get('page') || 1));
  return {rows: items.slice((at - 1) * per, at * per), pagination: {page: at, per_page: per, total: items.length, has_next: at * per < items.length}};
};

// ---------------------------------------------------------------- CI
const REPOS = [
  {id: 17, repository: 'org/repo', default_branch: 'main', forge_url: 'https://github.com/org/repo'},
  {id: 18, repository: 'org/web-console', default_branch: 'main', forge_url: 'https://github.com/org/web-console'},
  {id: 19, repository: 'org/infra', default_branch: 'trunk', forge_url: 'https://github.com/org/infra'},
  {id: 20, repository: 'org/docs-site', default_branch: 'main', forge_url: 'https://github.com/org/docs-site'},
];
const TITLES = {
  17: ['Fix scheduler leak on host reconnect', 'Add retry budget to dispatch queue', 'Bump msgpack to 1.1.2', 'Refactor inspector stream backpressure', 'Handle empty mission plan on resume', 'Tighten gateway token expiry check', 'Cache fleet roster between polls'],
  18: ['Redesign attention inbox rows', 'Fix sticky header overlap on mobile', 'Lazy-load analytics charts', 'Update Playwright to 1.51'],
  19: ['Rotate beast TLS certificate', 'Add nibbler to inventory', 'Pin woodpecker agent image'],
  20: ['Document review gate states'],
};
// Per project: [daysAgo, hour offset, state, branch, event, minutes, title index]. Deterministic, includes failures, a running run and a killed one.
function runsFor(ctx) {
  const rows = [];
  const plan = {
    17: [[0.02, 'running', 'feature/queue-retry', 'pull_request', 0, 1], [0.15, 'failure', 'main', 'push', 11, 0], [0.4, 'success', 'main', 'push', 9, 4], [0.9, 'success', 'feature/queue-retry', 'pull_request', 8, 1],
      [1.2, 'failure', 'feature/queue-retry', 'pull_request', 7, 1], [1.8, 'success', 'main', 'push', 10, 3], [2.4, 'success', 'main', 'push', 9, 6], [3.1, 'failure', 'main', 'push', 12, 5], [3.6, 'success', 'main', 'push', 9, 2],
      [4.5, 'killed', 'fix/token-expiry', 'pull_request', 3, 5], [5.2, 'success', 'main', 'push', 10, 4], [6.0, 'success', 'main', 'push', 9, 3], [7.4, 'failure', 'main', 'push', 13, 0], [8.8, 'success', 'main', 'push', 9, 6], [10.2, 'success', 'main', 'push', 10, 2], [12.5, 'success', 'main', 'push', 9, 4]],
    18: [[0.3, 'success', 'main', 'push', 6, 0], [0.8, 'success', 'main', 'push', 6, 2], [1.6, 'failure', 'fix/header-overlap', 'pull_request', 5, 1], [2.9, 'success', 'main', 'push', 7, 3], [4.2, 'success', 'main', 'push', 6, 0], [6.5, 'success', 'main', 'push', 6, 1], [9.1, 'success', 'main', 'push', 7, 2], [11.7, 'success', 'main', 'push', 6, 3]],
    19: [[0.6, 'success', 'trunk', 'push', 4, 0], [2.2, 'failure', 'trunk', 'push', 3, 1], [3.3, 'success', 'trunk', 'push', 4, 2], [5.8, 'pending', 'feature/nibbler', 'pull_request', 0, 1], [8.0, 'success', 'trunk', 'push', 4, 0]],
    20: [[1.1, 'success', 'main', 'push', 2, 0], [6.1, 'success', 'main', 'push', 2, 0]],
  };
  for (const repo of REPOS) {
    const list = plan[repo.id];
    list.forEach(([days, state, branch, event, minutes, title], index) => {
      const number = 240 - index * 3 - (repo.id - 17) * 40;
      const created = Math.round(days * D);
      const done = state === 'running' || state === 'pending';
      rows.push({
        provider_url: PROVIDER, provider_repo_id: repo.id, repository: repo.repository, run_number: number, revision: SHA(number),
        branch, event, ref: event === 'pull_request' ? `refs/pull/${100 + number % 50}/head` : `refs/heads/${branch}`, state,
        title: TITLES[repo.id][title], created_at: ctx.ago(created), started_at: ctx.ago(created - 5),
        finished_at: done ? null : ctx.ago(created - minutes * 60), updated_at: ctx.ago(created - minutes * 60),
        provider_url_run: `${PROVIDER}/repos/${repo.id}/pipeline/${number}`,
        url: `${PROVIDER}/repos/${repo.id}/pipeline/${number}`, default_branch: repo.default_branch, forge_url: repo.forge_url,
        project_id: `proj-${repo.id}`, can_retry: state === 'failure' || state === 'killed', can_cancel: done,
        ...(repo.id === 17 && index < 3 ? {mission_id: IDS.piMissions[index % 2], session_id: IDS.sessions[index % 3],
          related_missions: [{mission_id: IDS.piMissions[index % 2]}], related_sessions: [{session_id: IDS.sessions[index % 3]}]} : {}),
      });
    });
  }
  return rows;
}
const ciList = ctx => {
  const runs = runsFor(ctx), pipelines = {};
  for (const run of runs) (pipelines[run.repository] ||= []).push(run);
  return {pipelines, repos: REPOS.map(r => ({...r, provider_url: PROVIDER, provider_repo_id: r.id})), status: 'ready', available: true, stale: false, refreshing: false,
    last_success_at: ctx.ago(40), repo_errors: {}, progress: {total: 4, completed: 4, current: ''}, can_act: true, agents: [
      {name: 'woodpecker-beast-1', platform: 'linux/amd64', capacity: 4, backend: 'docker', last_contact: ctx.ago(20)},
      {name: 'woodpecker-bender-1', platform: 'linux/arm64', capacity: 2, backend: 'docker', last_contact: ctx.ago(45)}]};
};
function outcomeCounts(rows) {
  const outcomes = {};
  for (const r of rows) outcomes[r.state] = (outcomes[r.state] || 0) + 1;
  const success = outcomes.success || 0, failure = outcomes.failure || 0, completed = success + failure;
  const durations = rows.filter(r => r.finished_at && ['success', 'failure'].includes(r.state)).map(r => Math.round((Date.parse(r.finished_at) - Date.parse(r.started_at)) / 1000));
  durations.sort((a, b) => a - b);
  const at = q => durations[Math.ceil(durations.length * q) - 1];
  return {pipeline_count: rows.length, outcomes, completed_count: completed, success_count: success, failure_count: failure,
    pass_rate: completed ? success / completed : null, failure_rate: completed ? failure / completed : null, active_count: (outcomes.running || 0) + (outcomes.pending || 0),
    duration_seconds: {sample_count: durations.length, p50: durations.length >= 5 ? at(.5) : null, p95: durations.length >= 5 ? at(.95) : null,
      mean: durations.length ? durations.reduce((a, b) => a + b, 0) / durations.length : null, insufficient_data: durations.length < 5}};
}
function ciSummary(ctx, params) {
  const days = Number(params.get('days') || 14) || 14, branch = params.get('branch') || null, repository = params.get('repository') || null;
  const cutoff = Date.parse(ctx.ago(days * D));
  let runs = runsFor(ctx).filter(r => Date.parse(r.created_at) >= cutoff && (!branch || r.branch === branch) && (!repository || r.repository === repository));
  if (params.get('scope') === 'pr') runs = runs.filter(r => r.event === 'pull_request');
  if (params.get('scope') === 'default') runs = runs.filter(r => r.event !== 'pull_request' && r.branch === (REPOS.find(p => p.id === r.provider_repo_id) || {}).default_branch);
  const projects = REPOS.filter(p => !repository || p.repository === repository).map(p => {
    const mine = runs.filter(r => r.provider_repo_id === p.id);
    return {project_id: JSON.stringify([PROVIDER, String(p.id), p.repository]), provider_url: PROVIDER, provider_repo_id: String(p.id), repository: p.repository,
      ...outcomeCounts(mine), unknown_time_run_count: 0, scope_unavailable: false, coverage: {complete: true, exhausted: true}};
  });
  const byDay = {};
  for (const r of runs) (byDay[r.created_at.slice(0, 10)] ||= []).push(r);
  return {projects, kpis: {project_count: projects.length, ...outcomeCounts(runs)}, charts: Object.keys(byDay).sort().map(day => ({day, ...outcomeCounts(byDay[day])})),
    available: true, status: 'ready', refreshing: false, revision: 'sum-1', generated_at: ctx.ago(40),
    scope: {kind: 'retained_observations', complete: false, partial: false, stale: false, truncated: false, memory_only: false, persistence: 'durable_sink', days, mode: 'days', run_number: null,
      window: {start: ctx.ago(days * D), end: ctx.now}, repository, branch, scope: params.get('scope') || 'all', minimum_percentile_samples: 5, denominator: 'successful_or_failed_runs', time_basis: 'created_at'}};
}
function ciHistory(ctx) {
  return {available: true, status: 'ready', stale: false, refreshing: false, repos: REPOS.map(r => ({...r, provider_url: PROVIDER})), pipelines: runsFor(ctx),
    coverage: Object.fromEntries(REPOS.map(r => [String(r.id), {complete_since: ctx.ago(20 * D), exhausted: true, truncated: false}])), scope: {kind: 'provider_history', complete: true}};
}
const TESTS = [
  {name: 'TestDispatchRetriesWithBackoff', classname: 'scheduler', file: 'internal/scheduler/dispatch_test.go', fails: 6, total: 61, flaky: true, last: 1840, message: 'expected 3 attempts, got 2', trace: 'dispatch_test.go:142: retry budget exhausted\n  at scheduler.(*Queue).Dispatch (queue.go:88)'},
  {name: 'TestHostReconnectReleasesLease', classname: 'scheduler', file: 'internal/scheduler/lease_test.go', fails: 4, total: 58, flaky: false, last: 920, message: 'lease still held after reconnect', trace: 'lease_test.go:77: leaked lease host=bender'},
  {name: 'TestInspectorStreamBackpressure', classname: 'inspector', file: 'internal/inspector/stream_test.go', fails: 3, total: 60, flaky: true, last: 2650, message: 'timeout waiting for drain', trace: ''},
  {name: 'TestTokenExpiryRejectsSkew', classname: 'gateway', file: 'internal/gateway/token_test.go', fails: 2, total: 58, flaky: false, last: 40, message: 'accepted token 31s past expiry', trace: ''},
  {name: 'TestFleetRosterCache', classname: 'fleet', file: 'internal/fleet/roster_test.go', fails: 0, total: 61, flaky: false, last: 12, message: '', trace: ''},
  {name: 'TestMissionPlanResumeEmpty', classname: 'mission', file: 'internal/mission/resume_test.go', fails: 0, total: 59, flaky: false, last: 31, message: '', trace: ''},
  {name: 'renders attention rows with long titles that wrap onto a second line without clipping the action buttons', classname: 'web-console', file: 'src/attention/rows.spec.ts', fails: 1, total: 22, flaky: false, last: 410, message: 'locator resolved to 0 elements', trace: ''},
];
function ciTests(ctx, params) {
  const repository = params.get('repository') || 'org/repo';
  const repo = REPOS.find(r => r.repository === repository) || REPOS[0];
  const tests = repo.id === 17 ? TESTS.slice(0, 6) : repo.id === 18 ? TESTS.slice(6) : [];
  return {tests: tests.map((t, i) => ({id: `${repo.repository}::${t.classname}::${t.name}`, name: t.name, classname: t.classname, suite: t.classname, file: t.file, branch: 'main',
    provider_url: PROVIDER, provider_repo_id: String(repo.id), repository: repo.repository, failure_count: t.fails, completed_count: t.total,
    classification: t.flaky ? 'proven_flaky' : t.fails ? 'inconsistent_outcomes' : 'consistent', identity_ambiguous: false,
    first_failure: t.fails ? ctx.ago((9 - i) * D) : null, last_pass: ctx.ago(3 * H + i * 600), last_duration_ms: t.last,
    duration_ms: {sample_count: t.total, p50: Math.round(t.last * .8), p95: Math.round(t.last * 1.6), insufficient_data: false},
    failure_evidence: t.fails ? {message: t.message, trace: t.trace} : null,
    representative_runs: t.fails ? [{provider_url: PROVIDER, provider_repo_id: String(repo.id), repository: repo.repository, run_number: 237 - i, revision: SHA(237 - i), branch: 'main'}] : []})),
  available: true, status: 'ready',
  scope: {kind: 'retained_observations', complete: false, partial: true, truncated: false, stale: false, days: 14, mode: 'days', minimum_percentile_samples: 5, not_reported_run_count: 3, test_count: tests.length, returned_test_count: tests.length, omitted_test_count: 0}};
}
function ciReports(ctx) {
  const runs = runsFor(ctx).filter(r => ['success', 'failure'].includes(r.state) && r.provider_repo_id <= 18);
  return {reports: runs.map((r, i) => {
    const failed = r.state === 'failure' ? 2 + (i % 3) : 0, total = 180 + i;
    return {report_id: `rep-${r.provider_repo_id}-${r.run_number}`, provider_url: PROVIDER, provider_repo_id: String(r.provider_repo_id), repository: r.repository, run_number: r.run_number,
      revision: r.revision, branch: r.branch, reported_at: r.finished_at, ingested_at: r.finished_at, attempt: 1, source: i % 5 === 4 ? 'log_parse' : 'junit',
      summary: {passed: total - failed - 4, failed, errored: 0, skipped: 4, total, duration_ms: 41000 + i * 700}, runners: [i % 5 === 4 ? 'go test' : 'junit'], index_complete: true, skipped_steps: []};
  }), pipelines: runs,
  repos: REPOS.map(r => ({...r, provider_url: PROVIDER, provider_repo_id: r.id})),
  scope: {kind: 'retained_observations', complete: false, truncated: false, persistence: 'durable_sink', report_count: runs.length, pipeline_count: runs.length, test_details: 'omitted'}};
}
function ciPerformance(ctx, params) {
  const days = Number(params.get('days') || 14);
  const identity = {repository: 'org/repo', branch: 'main', provider_url: PROVIDER, provider_repo_id: '17', project_id: 'proj-17', revision: SHA(237)};
  const samples = [0, 1, 2, 3, 4, 5, 6, 7].map(i => ({...identity, operation_id: `provider:17:${237 - i}`, record_type: 'provider-execution', observed_at: ctx.ago((i + 1) * 20 * H), wall_seconds: 520 + i * 37 + (i % 3) * 60, phase: 'pipeline', measurement_source: 'provider_execution'}));
  const stats = (values, n = values.length) => ({sample_count: n, p50: values[Math.floor(values.length / 2)], p95: values[values.length - 1], mean: values.reduce((a, b) => a + b, 0) / values.length, insufficient_data: n < 5});
  const w = samples.map(s => s.wall_seconds).sort((a, b) => a - b);
  return {schema_version: 1, days, window: {start: ctx.ago(days * D), end: ctx.now},
    cache_groups: [{...identity, kind: 'go-build', sample_count: 42, hit_count: 31, restore_count: 21, hit_rate: 31 / 42 > .5 ? 0.74 : 0.4, measurement_source: 'cache_operation', restore: stats([4, 5, 6, 7, 9, 12, 38]), save: stats([8, 9, 11, 13, 14, 15, 22]), insufficient_data: false},
      {...identity, repository: 'org/web-console', provider_repo_id: '18', project_id: 'proj-18', kind: 'pnpm-store', sample_count: 12, hit_count: 4, restore_count: 12, hit_rate: 0.33, measurement_source: 'cache_operation', restore: stats([11, 14, 16, 22, 61, 88]), save: stats([3], 3), insufficient_data: false}],
    execution_trends: [{...identity, ...stats(w), baseline: stats([480, 500, 515, 530, 560, 590]), phase: 'pipeline', measurement_source: 'provider_execution', samples}],
    alerts: [{...identity, rule: 'execution_regression', kind: null, metric: 'execution_p95', phase: 'pipeline', measurement_source: 'provider_execution', current_value: 820, baseline_value: 590, sample_count: 8, baseline_sample_count: 6, window: {start: ctx.ago(days * D), end: ctx.now}},
      {...identity, repository: 'org/web-console', provider_repo_id: '18', project_id: 'proj-18', rule: 'low_hit_rate', kind: 'pnpm-store', metric: 'hit_rate', phase: null, measurement_source: 'cache_operation', current_value: 0.33, baseline_value: 0.5, sample_count: 12, baseline_sample_count: 0, window: {start: ctx.ago(days * D), end: ctx.now}}],
    scope: {kind: 'retained_observations', complete: false, partial: true, stale: false, persistence: 'durable_sink', memory_only: false, record_limit: 2000},
    coverage: {complete: false, partial: true, latest_observed_at: ctx.ago(20 * H), selected_sample_count: 62, minimum_percentile_samples: 5}, project_mapping_status: 'ok', project_mapping_error: ''};
}
function ciPipeline(ctx, runNumber) {
  const run = runsFor(ctx).find(r => r.run_number === runNumber) || runsFor(ctx)[0];
  const failed = run.state === 'failure';
  const step = (id, name, state, from, to, deps = []) => ({id, pid: id, name, state, depends_on: deps, started: epoch(ctx, from), finished: to == null ? null : epoch(ctx, to)});
  return {...run, workflows: [{id: 1, name: 'build-and-test', state: run.state, children: [
    step(11, 'clone', 'success', 600, 590), step(12, 'build', 'success', 590, 470, ['clone']),
    step(13, 'unit tests', failed ? 'failure' : 'success', 470, 330, ['build']), step(14, 'browser tests', 'success', 470, 360, ['build']),
    step(15, 'publish image', failed ? 'skipped' : 'success', 330, failed ? null : 300, ['unit tests', 'browser tests'])]}]};
}
const CI_LOG = ['go: downloading modules', '=== RUN   TestDispatchRetriesWithBackoff', '    dispatch_test.go:142: retry budget exhausted', '--- FAIL: TestDispatchRetriesWithBackoff (1.84s)',
  '=== RUN   TestFleetRosterCache', '--- PASS: TestFleetRosterCache (0.01s)', 'FAIL', 'FAIL\torg/repo/internal/scheduler\t3.2s'];

// ---------------------------------------------------------------- reviews
function reviews(ctx) {
  const row = (n, kind, status, decision, title, hours, extra = {}) => ({review_id: `rv-${n}`, id: `rv-${n}`, kind, status, decision, title, repository: 'org/repo',
    branch: 'feature/queue-retry', revision: SHA(60 + n), review_url: `https://plannotator.herdr.example/r/rv-${n}`, mission_id: IDS.piMissions[n % 4], session_id: IDS.sessions[n % 4],
    pull_request_number: extra.pr ?? null, summary: extra.summary || '', created_at: ctx.ago(hours * H + 1800), updated_at: ctx.ago(hours * H), ...(status === 'approved' || status === 'changes_requested' || status === 'failed' ? {completed_at: ctx.ago(hours * H)} : {}), reviewer: extra.reviewer || 'operator'});
  return [
    row(1, 'plan', 'awaiting_review', null, 'Plan: add retry budget to dispatch queue', 0.7, {summary: 'Three steps, touches scheduler and gateway. Needs a decision on the backoff ceiling.'}),
    row(2, 'code', 'awaiting_review', null, 'Code review: queue retry budget (PR 7)', 2, {pr: 7, summary: 'Adds RetryBudget, wires it into Dispatch, 14 files changed.'}),
    row(3, 'code', 'changes_requested', 'changes_requested', 'Code review: token expiry skew', 5, {pr: 9, summary: 'Reviewer asked for a boundary test at exactly 30s of skew.', reviewer: 'bender-reviewer'}),
    row(4, 'plan', 'approved', 'approved', 'Plan: inspector stream backpressure', 20, {summary: 'Approved with a note to keep the buffer bounded.'}),
    row(5, 'code', 'approved', 'approved', 'Code review: roster cache', 28, {pr: 6}),
    row(6, 'code', 'closed', null, 'Code review: abandoned lease refactor', 52, {pr: 4, summary: ''}),
    row(7, 'plan', 'approved', 'approved', 'Plan: empty mission resume', 80, {}),
    row(8, 'code', 'changes_requested', 'changes_requested', 'Code review: sticky header overlap on narrow screens with a deliberately long title that should wrap', 120, {pr: 12, reviewer: 'zapp-reviewer'}),
    row(9, 'code', 'failed', null, 'Code review: msgpack bump', 150, {pr: 8}),
  ];
}

// ---------------------------------------------------------------- review gate
function gatePrs() {
  const key = n => `github.com/org/repo#${n}`;
  return [
    {key: key(7), state: 'reviewing', review_rounds: 1, ci_fix_rounds: 0, head_sha: SHA(7), reason: '', approved_sha: '', last_findings: []},
    {key: key(9), state: 'changes_requested', review_rounds: 2, ci_fix_rounds: 1, head_sha: SHA(9), reason: 'Two blocking findings remain.', approved_sha: '', last_findings: [
      {severity: 'high', path: 'internal/gateway/token.go', line: 88, message: 'Clock skew tolerance is applied after the expiry comparison, so a token 31s past expiry is accepted.', required_change: 'Compare against exp + skew, not exp, and add a boundary test.'},
      {severity: 'medium', path: 'internal/gateway/token_test.go', line: 40, message: 'Test uses time.Now() directly and is flaky near second boundaries.', required_change: 'Inject a clock.'},
      {severity: 'low', path: 'README.md', line: 12, message: 'Typo.', required_change: ''}]},
    {key: key(11), state: 'ci_red', review_rounds: 0, ci_fix_rounds: 2, head_sha: SHA(11), reason: 'unit tests failed twice', approved_sha: '', last_findings: []},
    {key: key(12), state: 'ci_pending', review_rounds: 0, ci_fix_rounds: 0, head_sha: SHA(12), reason: '', approved_sha: '', last_findings: []},
    {key: key(13), state: 'review_queued', review_rounds: 0, ci_fix_rounds: 0, head_sha: SHA(13), reason: '', approved_sha: '', last_findings: []},
    {key: key(6), state: 'approved', review_rounds: 1, ci_fix_rounds: 0, head_sha: SHA(6), reason: '', approved_sha: SHA(6), last_findings: []},
    {key: key(5), state: 'merged', review_rounds: 2, ci_fix_rounds: 0, head_sha: SHA(5), reason: '', approved_sha: SHA(5), last_findings: []},
    {key: key(4), state: 'superseded', review_rounds: 1, ci_fix_rounds: 0, head_sha: SHA(4), reason: 'Replaced by #6', approved_sha: '', last_findings: []},
    {key: key(3), state: 'escalated', review_rounds: 3, ci_fix_rounds: 3, head_sha: SHA(3), reason: 'Round limit reached; needs a person.', approved_sha: '', last_findings: []},
    {key: 'github.com/org/web-console#31', state: 'ci_green', review_rounds: 0, ci_fix_rounds: 0, head_sha: SHA(31), reason: '', approved_sha: '', last_findings: []},
  ];
}
const gateQueue = ctx => ({depth: 2, jobs: [
  {key: 'github.com/org/repo#7', sha: SHA(7), state: 'claimed', author_model: 'claude-sonnet-5-5', enqueued_at: epoch(ctx, 14 * 60)},
  {key: 'github.com/org/repo#13', sha: SHA(13), state: 'queued', author_model: 'qwen3.6-35b-a3b', enqueued_at: epoch(ctx, 3 * 60)}],
gate: {counts: {repos: 3, prs: 10, enqueued: 2, merged: 1, errors: 0, skipped_drafts: 1, main_errors: 0}, last_tick_at: epoch(ctx, 30), last_error: ''}});
const gateNotices = ctx => [
  {kind: 'review_requested', channel: 'herdr', ok: true, key: 'github.com/org/repo#7', sha: SHA(7), at: epoch(ctx, 14 * 60)},
  {kind: 'changes_requested', channel: 'github', ok: true, key: 'github.com/org/repo#9', sha: SHA(9), at: epoch(ctx, 2 * H)},
  {kind: 'merged', channel: 'github', ok: true, key: 'github.com/org/repo#5', sha: SHA(5), at: epoch(ctx, 7 * H)},
  {kind: 'escalated', channel: 'slack', ok: false, key: 'github.com/org/repo#3', sha: SHA(3), at: epoch(ctx, 20 * H)}];
const gateMain = () => ({repos: [
  {repo: 'org/repo', branch: 'main', state: 'red', head_sha: SHA(240), last_green_sha: SHA(236), failures: ['unit tests'], range_total: 3, culprits: [SHA(238), SHA(239)]},
  {repo: 'org/web-console', branch: 'main', state: 'green', head_sha: SHA(88), last_green_sha: SHA(88), failures: [], range_total: 0, culprits: []},
  {repo: 'org/infra', branch: 'trunk', state: 'pending', head_sha: SHA(41), last_green_sha: SHA(40), failures: [], range_total: 1, culprits: []}]});

// ---------------------------------------------------------------- deployments
function deployments(ctx) {
  const ev = (outcome, step, from, to, ago, host, extra = {}) => ({outcome, step, from, to, received_at: ctx.ago(ago), host_ts: ctx.ago(ago + 2), reported_by: host, host, ...extra});
  const NEW = SHA(240), OLD = SHA(236);
  return {
    components: {
      'gateway-beast': {running: NEW, target: NEW, step: 'health', health: 'healthy', last_result: {outcome: 'ok', received_at: ctx.ago(2 * H)}, timeline: [
        ev('started', 'pull', OLD, NEW, 2.3 * H, 'beast'), ev('ok', 'restart', OLD, NEW, 2.1 * H, 'beast'), ev('ok', 'health', OLD, NEW, 2 * H, 'beast', {evidence: 'probe ok: 3 models answered'})]},
      'nodes-bender': {running: OLD, target: NEW, step: 'restart', health: 'failed', last_result: {outcome: 'failed', received_at: ctx.ago(1.5 * H)}, timeline: [
        ev('started', 'pull', OLD, NEW, 1.7 * H, 'bender'), ev('failed', 'restart', OLD, NEW, 1.5 * H, 'bender', {evidence: 'unit herdr-node.service failed: exit-code 137 (OOM) after 3 restarts'})]},
      'gateway-zapp': {running: OLD, target: NEW, step: '', health: 'unknown', last_result: null, timeline: []},
      'dashboard-nibbler': {running: SHA(230), target: NEW, step: 'pull', health: 'stale', last_result: {outcome: 'ok', received_at: ctx.ago(3 * D)}, timeline: [
        ev('ok', 'health', SHA(228), SHA(230), 3 * D, 'nibbler', {evidence: ''})]},
    },
    definitions: {configured: true, groups: [{stage: 1, order: 0, components: ['gateway-beast', 'nodes-bender']}, {stage: 2, order: 0, components: ['gateway-zapp', 'dashboard-nibbler']}], errors: []},
    plans: [{track: {repo: 'org/repo', branch: 'main'}, target_commit: NEW, generation: 7, plan_commit: NEW, current: {type: 'Halt', stage: 1, order: 1, components: ['nodes-bender'], reason: 'nodes-bender failed at restart; stage 2 will not start.', remaining_s: 0, new_target: null, generation: 7},
      permitted: ['gateway-beast'], missing: [], components: {
        'gateway-beast': {state: 'healthy', outcome: 'ok', hold_remaining_s: 0, attempts: 1, repair_attempts: 0},
        'nodes-bender': {state: 'failed', outcome: 'failed', hold_remaining_s: 0, attempts: 3, repair_attempts: 1},
        'gateway-zapp': {state: 'waiting', outcome: null, hold_remaining_s: 0, attempts: 0, repair_attempts: 0},
        'dashboard-nibbler': {state: 'waiting', outcome: 'ok', hold_remaining_s: 900, attempts: 0, repair_attempts: 0}}, evidence_status: {}}],
    coordinator: {},
    escalations: [{component: 'nodes-bender', release: NEW, open: true, issue: {number: 482, state: 'open', url: 'https://github.com/org/repo/issues/482'}, pr: null, assistant: {state: 'working'},
      rounds: [{index: 1, finding: {kind: 'oom', source: 'host', summary: 'herdr-node.service killed by the kernel OOM reaper during model warm-up.'}, repair: {action: 'rollback', reason: 'Fell back to the previous release.', rollback_to: OLD}, attempt: 1}],
      report: {what_changed: 'Release 240 raises the default context window from 131k to 262k.', current_state: 'nodes-bender runs the previous release; stage 2 is held.', tried: ['restart x3', 'rollback to 236'], finding_source: 'host journal', next_step: 'Lower the warm-up batch size or give bender more swap.', evidence: 'exit-code 137 on all 3 restarts'}}],
  };
}

// ---------------------------------------------------------------- GitHub
const GH_REPOS = [
  {repository_id: 'R_17', name_with_owner: 'org/repo', open_issue_count: 7, issues_complete: true, issues_loaded: 7, private: true, archived: false, issues_enabled: true},
  {repository_id: 'R_18', name_with_owner: 'org/web-console', open_issue_count: 4, issues_complete: true, issues_loaded: 4, private: false, archived: false, issues_enabled: true},
  {repository_id: 'R_19', name_with_owner: 'org/infra', open_issue_count: 2, issues_complete: true, issues_loaded: 2, private: true, archived: false, issues_enabled: true},
  {repository_id: 'R_20', name_with_owner: 'org/docs-site', open_issue_count: 0, issues_complete: true, issues_loaded: 0, private: false, archived: false, issues_enabled: true},
  {repository_id: 'R_21', name_with_owner: 'org/legacy-tools', open_issue_count: 0, issues_complete: false, issues_loaded: 0, private: false, archived: true, issues_enabled: false, error: ''},
  {repository_id: 'R_22', name_with_owner: 'org/payments-sandbox', open_issue_count: 0, issues_complete: false, issues_loaded: 0, private: true, archived: false, issues_enabled: true, error: 'Resource not accessible by integration'},
];
const ghBase = ctx => ({status: 'ready', account: {login: 'herdr-bot'}, connection_generation: 3, last_success_at: ctx.ago(4 * 60), stale: false, partial: false, refreshing: false, available: true,
  progress: {total: 6, completed: 6}, scope: {owners: ['org'], excluded_repositories: []}, rate_limit: {remaining: 4871}});
function ghIssues(ctx) {
  const issue = (n, repo, title, labels, hours, auto, extra = {}) => ({github_id: `I_${n}`, repository_id: GH_REPOS.find(r => r.name_with_owner === repo).repository_id, repository: repo, number: n, title, labels, assignees: extra.assignees || [], updated_at: ctx.ago(hours * H), state: 'open',
    url: `https://github.com/${repo}/issues/${n}`, body: extra.body || '', autospec: auto});
  const spec = (eligibility, state, extra = {}) => ({eligibility, state, managed: state !== 'none', reasons: [], context: 'standard', reasoning: 'medium', language: 'go', evidence_source: 'github_labels', ...extra});
  return [
    issue(482, 'org/repo', 'nodes-bender OOM on warm-up with 262k context', ['bug', 'auto-implement', 'ctx:large'], 1.5, spec('eligible', 'in_progress'), {assignees: ['herdr-bot'], body: 'Release 240 raised the default context window. bender (48GB) is killed by the OOM reaper during warm-up.\n\nRepro: deploy 240 to bender, watch herdr-node.service.'}),
    issue(471, 'org/repo', 'Add retry budget to dispatch queue', ['enhancement', 'auto-implement'], 6, spec('eligible', 'preparation')),
    issue(466, 'org/repo', 'Inspector stream stalls when the browser tab sleeps', ['bug'], 20, spec('ineligible', 'none', {reasons: ['Missing label: auto-implement']})),
    issue(459, 'org/repo', 'Decide the data retention window for CI observations', ['question', 'needs-decision'], 30, spec('blocked', 'blocked', {reasons: ['Waiting on a decision']}), {assignees: ['wohlgemuth']}),
    issue(455, 'org/repo', 'Token expiry skew test is flaky', ['bug', 'flaky-test', 'auto-implement'], 55, spec('eligible', 'preparation')),
    issue(442, 'org/repo', 'Document the review gate states', ['documentation'], 90, spec('ineligible', 'none', {reasons: ['Missing label: auto-implement']})),
    issue(431, 'org/repo', 'Support PostgreSQL 17 in the control plane migration runner', ['enhancement', 'auto-implement', 'lang:python'], 140, spec('eligible', 'preparation', {language: 'python'})),
    issue(212, 'org/web-console', 'Attention inbox rows clip long titles', ['bug', 'auto-implement', 'lang:typescript'], 3, spec('eligible', 'in_progress', {language: 'typescript'}), {assignees: ['herdr-bot']}),
    issue(209, 'org/web-console', 'Lazy-load analytics charts', ['performance'], 40, spec('ineligible', 'none')),
    issue(205, 'org/web-console', 'Header overlaps content on phones', ['bug', 'auto-implement', 'blocked'], 70, spec('blocked', 'blocked', {reasons: ['Depends on #212']}), {assignees: []}),
    issue(198, 'org/web-console', 'Dark mode contrast for status chips', ['design'], 200, spec('ineligible', 'none')),
    issue(61, 'org/infra', 'Rotate beast TLS certificate before expiry', ['ops', 'auto-implement'], 12, spec('eligible', 'preparation', {language: 'shell'})),
    issue(58, 'org/infra', 'Add nibbler to inventory', ['ops'], 100, spec('ineligible', 'none')),
  ];
}
const ghTab = (issues, tab) => issues.filter(i => tab === 'all' || !tab ? true : tab === 'autospec' ? i.autospec.managed || i.autospec.eligibility === 'eligible' : tab === 'preparation' ? i.autospec.state === 'preparation' : tab === 'in_progress' ? i.autospec.state === 'in_progress' : tab === 'blocked' ? i.autospec.state === 'blocked' : true);
function ghProjects(ctx) {
  const opt = [{id: 'o1', name: 'Backlog'}, {id: 'o2', name: 'In progress'}, {id: 'o3', name: 'In review'}, {id: 'o4', name: 'Done'}];
  return [
    {project_id: 'PVT_1', owner: 'org', number: 1, title: 'Herdr roadmap Q4', closed: false, item_count: 11, status_options: opt, complete: true, error: ''},
    {project_id: 'PVT_2', owner: 'org', number: 4, title: 'CI observability', closed: false, item_count: 5, status_options: opt, complete: true, error: ''},
    {project_id: 'PVT_3', owner: 'org', number: 2, title: 'Legacy tracker (no status field)', closed: true, item_count: 2, complete: true, error: ''},
  ];
}
function ghItems(id) {
  const it = (n, title, status, repo, num, type = 'Issue', state = 'OPEN') => ({item_id: `${id}-${n}`, title, url: `https://github.com/${repo}/${type === 'Issue' ? 'issues' : 'pull'}/${num}`, status_option: status[0], status: status[1], position: n, repository: repo, number: num, type, state});
  const B = ['o1', 'Backlog'], P = ['o2', 'In progress'], R = ['o3', 'In review'], Dn = ['o4', 'Done'];
  if (id === 'PVT_2') return [it(1, 'Test explorer from log parsing', Dn, 'org/repo', 430, 'PullRequest', 'MERGED'), it(2, 'Performance regression alerts', R, 'org/repo', 7, 'PullRequest'), it(3, 'Cache hit-rate panel', P, 'org/repo', 471), it(4, 'Retention window decision for observations with a deliberately long title that wraps in the card', B, 'org/repo', 459), it(5, 'Provider history coverage', B, 'org/repo', 455)];
  if (id === 'PVT_3') return [it(1, 'Old tracker item', ['', ''], 'org/legacy-tools', 3, 'Issue', 'CLOSED'), it(2, 'Another', ['', ''], 'org/legacy-tools', 5, 'Issue', 'CLOSED')];
  return [it(1, 'Retry budget in dispatch queue', P, 'org/repo', 471), it(2, 'Queue retry budget (PR)', R, 'org/repo', 7, 'PullRequest'), it(3, 'nodes-bender OOM', P, 'org/repo', 482), it(4, 'Attention inbox long titles', P, 'org/web-console', 212),
    it(5, 'Data retention decision', B, 'org/repo', 459), it(6, 'Roster cache', Dn, 'org/repo', 6, 'PullRequest', 'MERGED'), it(7, 'Token skew test flake', B, 'org/repo', 455), it(8, 'Rotate beast TLS', B, 'org/infra', 61),
    it(9, 'Inspector backpressure', Dn, 'org/repo', 5, 'PullRequest', 'MERGED'), it(10, 'Lazy charts', B, 'org/web-console', 209), it(11, 'Header overlap on phones', B, 'org/web-console', 205)];
}

// ---------------------------------------------------------------- pull request trail
function prTrail(ctx) {
  return {pull_request: {number: 7, repository: 'org/repo', url: 'https://github.com/org/repo/pull/7', status: 'open', branch: 'feature/queue-retry', opened_at: ctx.ago(2 * D)},
    state: 'linked', reason: '', sources: {sessions: 'ok', handoffs: 'ok', reviews: 'ok', ci: 'ok', git: 'ok'}, omitted: 0,
    sessions: [
      {session_id: IDS.sessions[0], name: 'queue-retry implementer', role: 'implementer', runtime: 'claude', model: 'claude-sonnet-5-5', host_id: IDS.hosts[0], started_at: ctx.ago(2 * D + H), rule: 'branch_match'},
      {session_id: IDS.sessions[1], name: 'queue-retry reviewer', role: 'reviewer', runtime: 'codex', model: 'gpt-5.5', host_id: IDS.hosts[1], started_at: ctx.ago(20 * H), rule: 'review_gate'}],
    inferred_sessions: [{session_id: IDS.sessions[2], name: 'scratch investigation', role: 'explorer', runtime: 'claude', model: 'claude-haiku-4-5', host_id: IDS.hosts[2], started_at: ctx.ago(2 * D + 3 * H), rule: 'path_overlap'}],
    ambiguous: [{session_id: IDS.sessions[3]}], handoffs: [], reviews: [], ci_runs: [],
    timeline: [
      {kind: 'session', at: ctx.ago(2 * D + H), text: 'Implementer session s-101 started on beast', ref: IDS.sessions[0], rule: 'branch_match'},
      {kind: 'pr_opened', at: ctx.ago(2 * D), text: 'Pull request 7 opened: Add retry budget to dispatch queue', url: 'https://github.com/org/repo/pull/7'},
      {kind: 'ci', at: ctx.ago(2 * D - 600), text: 'CI run 228 failed: unit tests', state: 'failure', url: `${PROVIDER}/repos/17/pipeline/228`},
      {kind: 'handoff', at: ctx.ago(1.5 * D), text: 'Handoff from s-101 to a fresh session after context reset', ref: IDS.sessions[0], problem: 'Handoff lacked the failing test name'},
      {kind: 'ci', at: ctx.ago(D), text: 'CI run 231 passed', state: 'success', url: `${PROVIDER}/repos/17/pipeline/231`},
      {kind: 'review', at: ctx.ago(20 * H), text: 'Review round 1: changes requested (2 findings)', state: 'changes_requested', ref: 'rv-2'},
      {kind: 'review', at: ctx.ago(6 * H), text: 'Review round 2 in progress', state: 'reviewing', ref: 'rv-2'}],
    signals: {merged_without_rework: {value: false, reason: 'One round of changes was requested.'}, ci_green_first_try: {value: false, reason: 'Run 228 failed before run 231 passed.'}, ci_reruns: {value: 1, reason: ''},
      cost_usd_estimate: {value: 4.62, reason: 'Estimated from token counts.'}, duration_s: {value: 172800, reason: ''}, handoff_problems: {value: 1, reason: 'One handoff lost context.'}, review_verdicts: {value: 'changes_requested', reason: ''}}};
}

// ---------------------------------------------------------------- artifacts
const ARTIFACTS = ctx => [
  {artifact_id: 'art-1', display_name: 'Queue retry design review', original_filename: 'queue-retry-review.html', artifact_type: 'html', mime_type: 'text/html', size_bytes: 48213, status: 'active', created_at: ctx.ago(3 * D), sha256: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', current_version_id: 'ver-1c'},
  {artifact_id: 'art-2', display_name: 'CI p95 duration trend', original_filename: 'ci-trend.png', artifact_type: 'image', mime_type: 'image/png', size_bytes: 312904, status: 'active', created_at: ctx.ago(5 * D), sha256: 'a'.repeat(64), current_version_id: 'ver-2a'},
  {artifact_id: 'art-3', display_name: 'LC-BinBase annotation audit with a very long display name that keeps going to test wrapping in the registry table', original_filename: 'audit.pdf', artifact_type: 'document', mime_type: 'application/pdf', size_bytes: 2203918, status: 'active', created_at: ctx.ago(9 * D), sha256: 'b'.repeat(64), current_version_id: 'ver-3a'},
  {artifact_id: 'art-4', display_name: 'fleet-inventory.csv', original_filename: 'fleet-inventory.csv', artifact_type: 'data', mime_type: 'text/csv', size_bytes: 1842, status: 'active', created_at: ctx.ago(12 * D), sha256: 'c'.repeat(64), current_version_id: 'ver-4a'},
  {artifact_id: 'art-5', display_name: 'Old incident timeline', original_filename: '', artifact_type: 'html', mime_type: 'text/html', size_bytes: 9012, status: 'archived', created_at: ctx.ago(40 * D), sha256: 'd'.repeat(64), current_version_id: 'ver-5a'},
  {artifact_id: 'art-6', display_name: 'Deployment 240 evidence', original_filename: 'deploy-240.html', artifact_type: 'html', mime_type: 'text/html', size_bytes: 22450, status: 'quarantined', created_at: ctx.ago(2 * H), sha256: 'f'.repeat(64), current_version_id: 'ver-6a'},
];
const RENDER_HTML = '<!doctype html><html><body style="font-family:system-ui;margin:24px"><h1>Queue retry design review</h1><p>The dispatcher retries a failed host dispatch up to <b>3 times</b> with exponential backoff. This review covers the retry budget and the lease-release path.</p><h2>Findings</h2><ol><li>The budget is per queue, not per host.</li><li>Backoff has no ceiling; a ceiling of 60s is proposed.</li><li>Leases are released on reconnect.</li></ol><table border="1" cellpadding="6"><tr><th>Attempt</th><th>Delay</th></tr><tr><td>1</td><td>2s</td></tr><tr><td>2</td><td>8s</td></tr><tr><td>3</td><td>32s</td></tr></table></body></html>';
const artifactSub = (ctx, id, tail, params) => {
  if (id !== 'art-1') return null;
  const [head, second, third] = tail;
  if (head === 'versions' && !second) return {versions: [
    {version_id: 'ver-1c', version_number: 3, state: 'ready', created_by: 'asst_1', created_at: ctx.ago(3 * H), parent_version_id: 'ver-1b', is_report: false},
    {version_id: 'ver-1b', version_number: 2, state: 'ready', created_by: 'agent:s-101', created_at: ctx.ago(2 * D), parent_version_id: 'ver-1a', is_report: false},
    {version_id: 'ver-1a', version_number: 1, state: 'ready', created_by: 'agent:s-101', created_at: ctx.ago(3 * D), parent_version_id: null, is_report: false}]};
  if (head === 'versions' && third === 'diff') return {files: [{name: 'queue-retry-review.html', change: 'modified', diff: '@@ -12,3 +12,4 @@\n-<li>Backoff has no ceiling.</li>\n+<li>Backoff has no ceiling; a ceiling of 60s is proposed.</li>\n+<li>Leases are released on reconnect.</li>'}, {name: 'assets/backoff.png', change: 'added', reason: 'binary file'}]};
  if (head === 'links') return {links: [{link_kind: 'project', project_id: 'project:atlas'}, {link_kind: 'issue', issue_ref: 'org/repo#471'}, {link_kind: 'task', task_id: 'T-13'}]};
  if (head === 'reviews') return {reviews: [{review_id: 'rv-2', state: 'awaiting_review', version_id: 'ver-1c', requested_by: 'asst_1'}, {review_id: 'rv-5', state: 'approved', version_id: 'ver-1a', reviewer: 'operator'}]};
  if (head === 'render') return {kind: 'html', html: RENDER_HTML, too_large: false};
  if (head === 'shares' && second && third === 'events') return {events: [{at: ctx.ago(H), kind: 'opened', actor: 'reviewer@example.org', detail: ''}, {at: ctx.ago(50 * 60), kind: 'annotated', actor: 'reviewer@example.org', detail: '1 annotation'}, {at: ctx.ago(20 * 60), kind: 'denied', actor: 'stranger@example.com', detail: 'Not on the allow list'}]};
  if (head === 'shares') return {shares: [
    {share_id: 'sh-1', title: 'Design review for the queue team', audience: 'allow_list', state: 'live', allow_emails: ['reviewer@example.org', 'lead@example.org'], allow_groups: [], version_id: 'ver-1c', expires_at: ctx.ago(-5 * D)},
    {share_id: 'sh-2', title: 'Org-wide read-only', audience: 'org', state: 'pending', allow_emails: [], allow_groups: ['eng'], version_id: 'ver-1c', expires_at: null},
    {share_id: 'sh-3', title: 'Expired preview', audience: 'public', state: 'revoked', allow_emails: [], allow_groups: [], version_id: 'ver-1a', expires_at: ctx.ago(2 * D)}]};
  if (head === 'collaborators') return {writable: true, writable_reason: '', collaborators: [
    {agent_id: 'agent:s-101', display_name: 'queue-retry implementer', permission: 'write', expires_at: ctx.ago(-2 * D), last_used_at: ctx.ago(3 * H), commits: 4, conflicts: 0},
    {agent_id: 'agent:s-102', display_name: 'reviewer (codex)', permission: 'comment', expires_at: null, last_used_at: ctx.ago(20 * H), commits: 0, conflicts: 1}],
  pending_invites: [{invite_id: 'inv-1', permission: 'write', expires_at: ctx.ago(-D)}]};
  if (head === 'collab-activity') return {attempts: [{outcome: 'committed', actor_id: 'agent:s-101', received_at: ctx.ago(3 * H), conflict_names: [], reason: ''},
    {outcome: 'conflict', actor_id: 'agent:s-102', received_at: ctx.ago(20 * H), conflict_names: ['queue-retry-review.html'], reason: 'Base version is stale'},
    {outcome: 'denied', actor_id: 'agent:s-104', received_at: ctx.ago(2 * D), conflict_names: [], reason: 'Read-only grant'}]};
  if (head === 'annotations') return {annotations: [
    {annotation_id: 'an-1', author_email: 'reviewer@example.org', author_role: 'reviewer', version_number: 3, anchor: {kind: 'text_quote', file: 'queue-retry-review.html', exact: 'Backoff has no ceiling', prefix: '<li>', suffix: '; a ceiling'}, body: 'Please justify 60s; the gateway drops idle connections at 45s.', status: 'open', created_at: ctx.ago(50 * 60), addressed_in_version_id: null},
    {annotation_id: 'an-2', author_email: 'lead@example.org', author_role: 'owner', version_number: 2, anchor: {kind: 'text_quote', file: 'queue-retry-review.html', exact: 'per queue, not per host', prefix: 'The budget is ', suffix: '.'}, body: 'Agreed, per host is safer.', status: 'addressed', created_at: ctx.ago(D), addressed_in_version_id: 'ver-1c'}], next: null};
  return null;
};

// ---------------------------------------------------------------- publications
const publications = ctx => ({publications: [
  {publication_id: 'pub-1', resource_id: 'art-1', pinned_version_id: 'ver-1c', audience: 'org', state: 'requested', desired_state: 'active', expires_at: ctx.ago(-30 * D)},
  {publication_id: 'pub-2', resource_id: 'art-2', pinned_version_id: 'ver-2a', audience: 'public', state: 'active', desired_state: 'active', expires_at: null},
  {publication_id: 'pub-3', resource_id: 'art-3', pinned_version_id: 'ver-3a', audience: 'allow_list', state: 'active', desired_state: 'active', expires_at: ctx.ago(-3 * D)},
  {publication_id: 'pub-4', resource_id: 'art-5', pinned_version_id: 'ver-5a', audience: 'org', state: 'revoked', desired_state: 'revoked', expires_at: ctx.ago(10 * D)}]});

// ---------------------------------------------------------------- work graph (teams, team map)
const TEAMS = [
  {native_id: 'atlas', display_name: 'Atlas platform', status: 'active', project_id: 'project:atlas', tombstone: false},
  {native_id: 'orion', display_name: 'Orion data pipelines', status: 'active', project_id: 'project:orion', tombstone: false},
  {native_id: 'ci-guild', display_name: 'CI guild', status: 'active', project_id: null, tombstone: false},
  {native_id: 'legacy', display_name: 'Legacy tools (wound down)', status: 'archived', project_id: 'project:legacy', tombstone: true},
];
function workSnapshot() {
  const n = (id, kind, display_name, status = 'active') => ({id, kind, display_name, native_id: id.split(':')[1], status, tombstone: false});
  const e = (id, type, from_id, to_id, live = true) => ({id, type, from_id, to_id, live});
  const nodes = [n('project:atlas', 'project', 'Atlas platform'), n('issue:org/repo#471', 'issue', 'Retry budget in dispatch queue', 'in_progress'), n('issue:org/repo#482', 'issue', 'nodes-bender OOM', 'in_progress'),
    n('task:T-12', 'task', 'Implement RetryBudget', 'done'), n('task:T-13', 'task', 'Wire budget into Dispatch', 'in_progress'), n('artifact:art-1', 'artifact', 'Queue retry design review'),
    n('review:rv-2', 'review', 'Code review PR 7', 'open'), n('decision:D-4', 'decision', 'Backoff ceiling is 60s'), n('agent_session:s-101', 'agent_session', 'queue-retry implementer'), n('host:beast', 'host', 'beast')];
  const edges = [e('e1', 'contains', 'project:atlas', 'issue:org/repo#471'), e('e2', 'contains', 'project:atlas', 'issue:org/repo#482'), e('e3', 'contains', 'issue:org/repo#471', 'task:T-12'), e('e4', 'contains', 'issue:org/repo#471', 'task:T-13'),
    e('e5', 'depends_on', 'task:T-13', 'task:T-12'), e('e6', 'produced', 'agent_session:s-101', 'artifact:art-1'), e('e7', 'reviews', 'review:rv-2', 'artifact:art-1'), e('e8', 'decided', 'decision:D-4', 'issue:org/repo#471'),
    e('e9', 'runs_on', 'agent_session:s-101', 'host:beast'), e('e10', 'works_on', 'agent_session:s-101', 'task:T-13', false)];
  return {nodes, edges, has_more: false};
}

// ---------------------------------------------------------------- dispatcher
export function answer(pathname, method, params, ctx) {
  const p = pathname.replace(/\/+$/, '');
  const get = method === 'GET';
  // CI
  if (p === '/api/v1/ci' && get) return ciList(ctx);
  if (p === '/api/v1/ci/history' && get) return ciHistory(ctx);
  if (p === '/api/v1/ci/summary' && get) return ciSummary(ctx, params);
  if (p === '/api/v1/ci/performance' && get) return ciPerformance(ctx, params);
  if (p === '/api/v1/ci/reports' && get) return ciReports(ctx);
  if (p === '/api/v1/ci/tests' && get) return ciTests(ctx, params);
  if (p === '/api/v1/ci/pipeline' && get) return ciPipeline(ctx, Number(params.get('run_number') || params.get('number') || 0));
  if (p === '/api/v1/ci/log' && get) return {lines: CI_LOG.map((text, i) => ({line: i + 1, text})), truncated: false};
  if (p === '/api/v1/ci/log-slice' && get) return {lines: CI_LOG.slice(1, 5).map((text, i) => ({line: i + 2, text})), start: 2, end: 5, matched: true};
  if (p === '/api/v1/ci/fix' && get) return {available: false, fixes: []};
  // reviews
  if (p === '/api/v1/reviews' && get) {
    const all = reviews(ctx), open = params.get('needs_decision') ? all.filter(r => r.status === 'awaiting_review' || r.status === 'changes_requested') : all;
    return {reviews: open, total: open.length, has_more: false, checked_at: ctx.now};
  }
  let m = p.match(/^\/api\/v1\/reviews\/([^/]+)$/);
  if (m && get) { const r = reviews(ctx).find(x => x.review_id === m[1]); return r ? {...r, comments: [{author: 'reviewer', body: 'Please add a boundary test.', at: ctx.ago(4 * H)}]} : undefined; }
  // review gate
  if (p === '/api/v1/review-gate/prs' && get) return gatePrs();
  if (p === '/api/v1/review-gate/queue' && get) return gateQueue(ctx);
  if (p === '/api/v1/review-gate/notices' && get) return gateNotices(ctx);
  if (p === '/api/v1/review-gate/main' && get) return gateMain();
  // deployments
  if (p === '/api/v1/deployments' && get) return deployments(ctx);
  // github
  if (p === '/api/v1/github' && get) {
    const q = (params.get('search') || '').toLowerCase();
    const {rows, pagination} = page(params, GH_REPOS.filter(r => !q || r.name_with_owner.includes(q)));
    return {...ghBase(ctx), repositories: rows, pagination};
  }
  if (p === '/api/v1/github/issues' && get) {
    const repo = params.get('repository'), q = (params.get('search') || '').toLowerCase();
    const scoped = ghIssues(ctx).filter(i => (!repo || i.repository === repo || i.repository_id === repo) && (!q || `${i.title} ${i.labels.join(' ')}`.toLowerCase().includes(q)));
    const {rows, pagination} = page(params, ghTab(scoped, params.get('tab') || 'all'));
    const c = t => ghTab(scoped, t).length;
    return {...ghBase(ctx), issues: rows.map(({body, ...i}) => i), counts: {all: c('all'), autospec: c('autospec'), preparation: c('preparation'), in_progress: c('in_progress'), blocked: c('blocked')}, pagination};
  }
  if (p === '/api/v1/github/issue' && get) {
    const issue = ghIssues(ctx).find(i => i.number === Number(params.get('number')) && (!params.get('repository') || i.repository === params.get('repository') || i.repository_id === params.get('repository')));
    return issue ? {issue, checked_at: ctx.ago(60), connection_generation: 3, account: {login: 'herdr-bot'}} : undefined;
  }
  if (p === '/api/v1/github/projects' && get) {
    const id = params.get('project'), projects = ghProjects(ctx);
    if (!id) return {projects, projects_sync: {status: 'ready', deferred: false, error: ''}};
    const project = projects.find(x => x.project_id === id);
    return project ? {project, items: ghItems(id)} : undefined;
  }
  // pull-request trail
  if (p === '/api/v1/pull-requests/org/repo/7/trail' && get) return prTrail(ctx);
  // artifacts
  if (p === '/api/v1/artifacts' && get) {
    const q = (params.get('q') || '').toLowerCase(), type = params.get('type'), status = params.get('status');
    return {artifacts: ARTIFACTS(ctx).filter(a => (!q || `${a.display_name} ${a.original_filename}`.toLowerCase().includes(q)) && (!type || a.artifact_type === type) && (!status || a.status === status))};
  }
  m = p.match(/^\/api\/v1\/artifacts\/for\/([^/]+)\/(.+)$/);
  if (m && get) return {artifacts: ARTIFACTS(ctx).slice(0, 2), next: null};
  m = p.match(/^\/api\/v1\/artifacts\/([^/]+)(?:\/(.*))?$/);
  if (m && get) {
    const id = decodeURIComponent(m[1]), tail = m[2] ? m[2].split('/') : [];
    const art = ARTIFACTS(ctx).find(a => a.artifact_id === id);
    if (!art) return undefined;
    if (!tail.length) return art;
    return artifactSub(ctx, id, tail, params) || undefined;
  }
  // publications
  if (p === '/api/v1/publications' && get) return publications(ctx);
  // work graph
  if (p === '/api/v1/graph/work/teams' && get) {
    const status = params.get('status');
    return {items: TEAMS.filter(t => !status || t.status === status), has_more: false};
  }
  if (p === '/api/v1/graph/work/snapshot' && get) return workSnapshot();
  if (p === '/api/v1/graph/work/layout' && get) return {document: {nodes: {}}, etag: 'lay-1'};
  return undefined;
}

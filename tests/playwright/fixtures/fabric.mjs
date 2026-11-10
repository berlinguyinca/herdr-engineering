// /api/v1 fixtures: fabric (hosts, fleet, scheduler, ai-sync, services, fabric services, LLM usage, analytics).
// See ../dashboard-fixtures.mjs for the contract. Everything is derived from fixed numbers and ctx.ago(): no clock reads, no randomness.
const GIB = 1024 ** 3;
const HOST_NAMES = { beast: 'beast', bender: 'bender', zapp: 'zapp', nibbler: 'nibbler' };

// A repeatable wobble in [0,1): the same input always gives the same number, so charts have shape but never change between runs.
const wob = (i, salt = 0) => ((((i + 1) * 7919 + salt * 104729) % 97) / 97);

// ---- hosts -----------------------------------------------------------------------------------------------------------------------------
const PINS = { herdr: '0.84.2', pi: '1.12.0', autospec: '3.4.1', pi_extensions: { 'pi-engineering': '2.9.0', 'pi-memory': '1.3.2', 'pi-browse': '0.6.0' } };
const VERSIONS = (over = {}) => ({ herdr: '0.84.2', herdr_eng: '0.84.2', pi: '1.12.0', autospec: '3.4.1', tailscale: '1.78.1',
  pi_extensions: { 'pi-engineering': '2.9.0', 'pi-memory': '1.3.2', 'pi-browse': '0.6.0' }, ...over });

function hostRecords(ago) {
  return [
    { host_id: 'beast', host_name: 'beast', tailnet_ip: '100.64.12.4', status: 'healthy', last_seen: ago(40), maintenance: false,
      latest: { at: ago(40), uptime_s: 41 * 86400 + 5 * 3600, agents_running: 4, cpu: 62.4, mem: 71.8, swap: 3.2, load1: 9.4, load5: 8.1, load15: 6.7,
        mem_used_b: 91.9 * GIB, mem_total_b: 128 * GIB, net_rx_bps: 1.8 * 1024 ** 2, net_tx_bps: 640 * 1024, disk: 87.1,
        gpus: [{ name: 'NVIDIA RTX 6000 Ada', util_pct: 78, vram_used_b: 38.2 * GIB, vram_total_b: 48 * GIB, temp_c: 71 },
          { name: 'NVIDIA RTX 6000 Ada', util_pct: 54, vram_used_b: 22.7 * GIB, vram_total_b: 48 * GIB, temp_c: 66 }],
        mounts: [{ path: '/', used_b: 412 * GIB, total_b: 931 * GIB, pct: 44.3 }, { path: '/data', used_b: 3.48 * 1024 * GIB, total_b: 4 * 1024 * GIB, pct: 87.1 }],
        top: { cpu: [{ command: 'llama-server --model qwen3-35b-a3b', user: 'herdr', cpu_pct: 312.5, mem_pct: 18.2, owner: 's-101' },
          { command: 'node packages/dashboard/server.js', user: 'herdr', cpu_pct: 41.7, mem_pct: 2.4, owner: 'svc-1' },
          { command: 'cargo build --release -p herdr-gateway', user: 'wohlgemuth', cpu_pct: 38.9, mem_pct: 4.1, owner: 's-103' },
          { command: 'postgres: herdr herdr [local] SELECT', user: 'postgres', cpu_pct: 12.2, mem_pct: 6.8, owner: null }],
        mem: [{ command: 'llama-server --model qwen3-35b-a3b', user: 'herdr', cpu_pct: 312.5, mem_pct: 18.2, owner: 's-101' },
          { command: 'postgres: checkpointer', user: 'postgres', cpu_pct: 0.4, mem_pct: 9.6, owner: null },
          { command: 'chrome --headless=new --remote-debugging-port=9222', user: 'wohlgemuth', cpu_pct: 6.1, mem_pct: 5.3, owner: 's-102' }] } },
      problems: [{ severity: 'warning', code: 'disk_high', message: '/data is 87% full; the nightly benchmark cache is the largest consumer' }], problem_count: 1,
      updates: { manager: 'apt', outdated: 14, security: 3, reboot_required: true, failed_units: ['snapd.service'],
        packages: [{ name: 'openssl', from: '3.0.13-0ubuntu3.4', to: '3.0.13-0ubuntu3.6' }, { name: 'linux-image-generic', from: '6.8.0-48', to: '6.8.0-51' },
          { name: 'libc6', from: '2.39-0ubuntu8.3', to: '2.39-0ubuntu8.4' }, { name: 'tailscale', from: '1.76.6', to: '1.78.1' }, { name: 'git', from: '1:2.43.0-1ubuntu7.1', to: '1:2.43.0-1ubuntu7.2' }] },
      versions: VERSIONS(), pins: PINS, fleet_update: { last_updated: ago(86400 * 2 + 3600) } },
    { host_id: 'bender', host_name: 'bender', tailnet_ip: '100.64.12.9', status: 'degraded', last_seen: ago(95), maintenance: false,
      latest: { at: ago(95), uptime_s: 9 * 86400 + 14 * 3600 + 120, agents_running: 2, cpu: 91.6, mem: 88.2, swap: 41.5, load1: 17.9, load5: 15.2, load15: 11.8,
        mem_used_b: 56.4 * GIB, mem_total_b: 64 * GIB, net_rx_bps: 220 * 1024, net_tx_bps: 95 * 1024,
        gpus: [{ name: 'NVIDIA RTX 4090', util_pct: 97, vram_used_b: 23.1 * GIB, vram_total_b: 24 * GIB, temp_c: 91 }],
        mounts: [{ path: '/', used_b: 188 * GIB, total_b: 465 * GIB, pct: 40.4 }],
        top: { cpu: [{ command: 'vllm serve qwen3-32b --tensor-parallel-size 1', user: 'herdr', cpu_pct: 540.1, mem_pct: 36.7, owner: 's-104' }], mem: [] } },
      problems: [{ severity: 'critical', code: 'gpu_temp', message: 'GPU 0 at 91 °C for 12 minutes' }, { severity: 'warning', code: 'swap_high', message: 'Swap use is 41%' }], problem_count: 2,
      updates: { manager: 'apt', outdated: 0, security: 0, reboot_required: false, failed_units: [], packages: [] },
      versions: VERSIONS({ pi: '1.11.4', pi_extensions: { 'pi-engineering': '2.8.1', 'pi-memory': '1.3.2' } }), pins: PINS, fleet_update: { last_updated: ago(86400 * 9) } },
    { host_id: 'zapp', host_name: 'zapp', tailnet_ip: '100.64.12.21', status: 'healthy', last_seen: ago(7260), maintenance: true,
      latest: { at: ago(7260), uptime_s: 188 * 86400, agents_running: 0, cpu: 4.1, mem: 22.6, swap: 0, load1: 0.2, load5: 0.3, load15: 0.3,
        mem_used_b: 3.6 * GIB, mem_total_b: 16 * GIB, disk: 31.9, mounts: [{ path: '/', used_b: 38 * GIB, total_b: 119 * GIB, pct: 31.9 }], top: { cpu: [], mem: [] } },
      problems: [], problem_count: 0,
      updates: { manager: 'apt', outdated: 31, security: 9, reboot_required: true, failed_units: [], packages: [] },
      versions: VERSIONS({ herdr: '0.83.9', herdr_eng: '0.83.9', autospec: '3.3.0' }), pins: PINS, fleet_update: null },
    { host_id: 'nibbler', host_name: 'nibbler', tailnet_ip: null, status: 'unknown', last_seen: null, maintenance: false, latest: null, problems: [], problem_count: 0, updates: null, versions: null, pins: PINS, fleet_update: null },
  ];
}

// A host's history: `n` samples ending now, one per `stepSeconds`. Each metric is its base plus a slow wave plus a little repeatable noise.
function telemetry(id, range, ago) {
  const spec = { '1h': [30, 120], '24h': [48, 1800], '7d': [56, 10800] }[range] || [30, 120];
  const [n, step] = spec;
  const base = { beast: [58, 70, 86, 62], bender: [84, 86, 40, 90], zapp: [4, 22, 32, 0], nibbler: null }[id];
  if (!base) return { samples: [] };
  const clamp = (v) => Math.round(Math.min(99.5, Math.max(0.5, v)) * 10) / 10;
  return { samples: Array.from({ length: n }, (_, i) => ({
    at: ago((n - 1 - i) * step + 40),
    cpu: clamp(base[0] + 14 * Math.sin(i / 4.5) + 8 * wob(i, 1)),
    mem: clamp(base[1] + 6 * Math.sin(i / 7) + 3 * wob(i, 2)),
    disk: clamp(base[2] + i * 0.03),
    gpu_util_pct: base[3] ? clamp(base[3] + 18 * Math.sin(i / 3) + 10 * wob(i, 3)) : null,
  })) };
}

function fleetInventory(ago) {
  const state = (version, ref) => (version == null ? { state: 'missing', version: null } : version === ref ? { state: 'ok', version } : { state: 'drifted', version });
  const row = (group, name, ref, b, d, z, n) => {
    const hosts = { beast: state(b, ref), bender: state(d, ref), zapp: state(z, ref), nibbler: state(n, ref) };
    const vals = Object.values(hosts);
    return { group, name, reference: ref, drifted: vals.some((v) => v.state === 'drifted'), missing: vals.some((v) => v.state === 'missing'), hosts };
  };
  const components = [
    row('tools', 'herdr', '0.84.2', '0.84.2', '0.84.2', '0.83.9', null),
    row('tools', 'pi', '1.12.0', '1.12.0', '1.11.4', '1.12.0', null),
    row('tools', 'autospec', '3.4.1', '3.4.1', '3.4.1', '3.3.0', null),
    row('tools', 'tailscale', '1.78.1', '1.78.1', '1.78.1', '1.78.1', '1.78.1'),
    row('tools', 'node', '22.11.0', '22.11.0', '22.11.0', '22.11.0', '22.11.0'),
    row('plugins', 'claude:superpowers', '5.2.0', '5.2.0', '5.2.0', '5.2.0', null),
    row('plugins', 'claude:oh-my-claudecode', '4.9.3', '4.9.3', '4.9.1', null, null),
    row('plugins', 'pi:pi-engineering', '2.9.0', '2.9.0', '2.8.1', '2.9.0', null),
    row('plugins', 'pi:pi-memory', '1.3.2', '1.3.2', '1.3.2', '1.3.2', null),
    row('plugins', 'codex:codex-review', '0.9.0', '0.9.0', null, null, null),
  ];
  return { hosts: [{ host: 'beast', collected_at: ago(310) }, { host: 'bender', collected_at: ago(420) }, { host: 'zapp', collected_at: ago(7300) }, { host: 'nibbler', collected_at: ago(86400 * 3) }],
    components, summary: { drifted: components.filter((c) => c.drifted).length, missing: components.filter((c) => c.missing).length } };
}

const fleetConfigStatus = () => ({ revision: 'c41f9d2e7a10b8365fd2', last_error: null,
  hosts: [{ id: 'beast', applied_revision: 'c41f9d2e7a10b8365fd2' }, { id: 'bender', applied_revision: '9be03a77c1d4e5f60a12' }, { id: 'zapp', applied_revision: null }] });

// ---- scheduler / ai-sync ------------------------------------------------------------------------------------------------------------------
const scheduler = () => ({ queued_agents: 3, claimed_executions: 7, hosts: [
  { host_uid: 'host_beast', host_name: 'beast', platform: 'linux/amd64', trusted: true, owned: true, reachable: true, policy: { mode: 'automatic', revision: 12 }, telemetry: { freshness_seconds: 18 }, capacity: { total_cpu: 64, total_memory_gib: 128 } },
  { host_uid: 'host_bender', host_name: 'bender', platform: 'linux/amd64', trusted: true, owned: true, reachable: true, policy: { mode: 'automatic', revision: 9 }, telemetry: { freshness_seconds: 41 }, capacity: { total_cpu: 32, total_memory_gib: 64 } },
  { host_uid: 'host_zapp', host_name: 'zapp', platform: 'linux/arm64', trusted: true, owned: true, reachable: true, policy: { mode: 'manual_only', revision: 4 }, telemetry: { freshness_seconds: 7260 }, capacity: { total_cpu: 8, total_memory_gib: 16 } },
  { host_uid: 'host_nibbler', host_name: null, platform: 'darwin/arm64', trusted: false, owned: false, reachable: false, policy: { mode: 'automatic', revision: 1 }, telemetry: { freshness_seconds: null }, capacity: {} },
] });

const aiSync = (ago) => ({ hosts: [
  { host_id: 'beast', ai_sync_version: '2.4.1', envs_enabled: ['claude', 'codex', 'pi'], last_sync_ts: ago(1260), ahead: 0, behind: 0, drifted_files_count: 0, drift_sample: [], skipped_links_count: 0, verify_ok: true, verify_errors_count: 0, verify_error_sample: [], last_error: null, checked_at: ago(120) },
  { host_id: 'bender', ai_sync_version: '2.4.1', envs_enabled: ['claude', 'pi'], last_sync_ts: ago(3 * 3600), ahead: 2, behind: 1, drifted_files_count: 3, drift_sample: ['.claude/settings.json', '.claude/skills/review/SKILL.md', '.pi/extensions/memory.json'], skipped_links_count: 2,
    verify_ok: false, verify_errors_count: 2, verify_error_sample: ['skills/review/SKILL.md: frontmatter missing description', 'hooks/pre-tool.sh: not executable'], last_error: null, checked_at: ago(180) },
  { host_id: 'zapp', ai_sync_version: '2.3.0', envs_enabled: ['claude'], last_sync_ts: ago(3 * 86400), ahead: null, behind: 14, drifted_files_count: 9, drift_sample: ['.claude/CLAUDE.md'], skipped_links_count: 0,
    verify_ok: null, last_error: 'git pull failed: could not resolve host github.com (network unreachable)', checked_at: ago(7300) },
  { host_id: 'nibbler', ai_sync_version: '', envs_enabled: [], last_sync_ts: null, checked_at: null },
] });

// ---- services -----------------------------------------------------------------------------------------------------------------------------
// state: what the page will derive for the row (kept here so the catalog counts match what serviceState() computes).
function serviceRecords(ago) {
  const obs = (state, age = 30) => ({ state, observed_at: ago(age), stale_after_seconds: 90 });
  const mk = (s) => ({ created_at: ago(86400 * 12), owner: null, description: '', url: null, ...s });
  return [
    { state: 'current', ...mk({ service_id: 'svc-1', service_name: 'herdr-dashboard', description: 'The operator dashboard served from the beast checkout, rebuilt on every merge.', owner: 'node (pid 41822)', host_id: 'beast', port: 4173, url: 'https://beast.fleet.example/dashboard', status: 'healthy', session_id: 's-101', updated_at: ago(45),
      catalog: { scope: 'herdr', scope_reason: 'Launched from pane s-101 on beast', label: 'Web application', purpose: 'Operator dashboard for the herdr fleet', project: 'herdr-engineering', project_id: 'herdr-engineering', repository_id: 'org/repo', commit_sha: '8d3f1ac', source_port: 3000, project_source: 'pane working directory', metadata_observation: obs('observed') },
      lifecycle: { state: 'current', last_seen_at: ago(45), reason: 'Reported healthy by its launch pane.' } }) },
    { state: 'current', ...mk({ service_id: 'svc-2', service_name: 'gateway-preview', description: 'Preview build of the unified gateway behind the fabric.', owner: 'cargo run', host_id: 'bender', port: 8088, url: 'https://gateway-preview.fabric.example', status: 'running', session_id: 's-103', updated_at: ago(120),
      catalog: { scope: 'herdr', scope_reason: 'Launched from pane s-103 on bender', label: 'API service', purpose: 'Exercise the gateway scheduling image before rollout', project: 'gateway', project_id: 'gateway', repository_id: 'org/repo', commit_sha: 'b72e90f', source_port: 8080, project_source: 'pane working directory', metadata_observation: obs('observed', 60) },
      lifecycle: { state: 'current', last_seen_at: ago(120), reason: 'Reported healthy by its launch pane.' } }) },
    { state: 'unknown', ...mk({ service_id: 'svc-3', service_name: 'storybook', description: 'Component explorer. The listener was not seen in the latest observation.', owner: 'node', host_id: 'beast', port: 6006, url: 'https://beast.fleet.example:6006', status: 'healthy', updated_at: ago(600),
      catalog: { scope: 'herdr', scope_reason: 'Launched from pane s-102 on beast', label: 'Web application', purpose: 'Browse and review UI components', project: 'herdr-engineering', project_id: 'herdr-engineering', repository_id: 'org/repo', source_port: 6006, project_source: 'pane working directory', metadata_observation: obs('missing', 400) },
      lifecycle: { state: 'current', last_seen_at: ago(600), reason: 'Status from the latest service report.' } }) },
    { state: 'unavailable', ...mk({ service_id: 'svc-4', service_name: 'benchmark-runner', description: 'Nightly peak-picker benchmark harness (stopped when its pane closed).', owner: 'python3', host_id: 'bender', port: 9100, url: 'https://bender.fleet.example:9100', status: 'stopped', updated_at: ago(5400),
      catalog: { scope: 'herdr', scope_reason: 'Launched from pane s-104 on bender', label: 'Batch worker', purpose: 'Run the three-tool peak picker benchmark', project: 'everything-bagel', project_id: 'everything-bagel', repository_id: 'berlinguyinca/everything-bagel', source_port: 9100, project_source: 'pane working directory', metadata_observation: obs('observed', 5400) },
      lifecycle: { state: 'unavailable', last_seen_at: ago(5400), unavailable_since: ago(4800), reason: 'The owning pane exited and the listener closed.' } }) },
    { state: 'archived', ...mk({ service_id: 'svc-5', service_name: 'docs-preview', description: '', host_id: 'zapp', port: 4000, status: 'stopped', updated_at: ago(86400 * 9),
      catalog: { scope: 'herdr', label: 'Web application', purpose: 'Preview of the rendered docs site', project: 'docs', project_id: null, repository_id: 'org/repo', source_port: 4000, metadata_observation: obs('observed', 86400 * 9) },
      lifecycle: { state: 'archived', archived: true, last_seen_at: ago(86400 * 9), unavailable_since: ago(86400 * 9), reason: 'Archived after nine days without a report.' } }) },
    { state: 'current', ...mk({ service_id: 'svc-6', service_name: 'tailscaled', description: 'Tailscale node daemon.', owner: 'tailscaled.service', host_id: 'beast', port: 41641, status: 'running', updated_at: ago(200),
      catalog: { scope: 'system', scope_reason: 'Confirmed as systemd unit tailscaled.service', label: 'System daemon', purpose: 'Private network for the fleet', endpoint_scope: 'local_only' },
      lifecycle: { state: 'current', last_seen_at: ago(200), reason: 'Unit active (running).' } }) },
    { state: 'current', ...mk({ service_id: 'svc-7', service_name: 'postgresql@16-main', description: 'Primary database for the control plane.', owner: 'postgresql@16-main.service', host_id: 'beast', port: 5432, status: 'active', updated_at: ago(180),
      catalog: { scope: 'system', scope_reason: 'Confirmed as systemd unit postgresql@16-main.service', label: 'Database', purpose: 'Control plane state', endpoint_scope: 'local_only' },
      lifecycle: { state: 'current', last_seen_at: ago(180), reason: 'Unit active (running).' } }) },
    { state: 'unavailable', ...mk({ service_id: 'svc-8', service_name: 'cups-browsed', description: '', owner: 'cups-browsed.service', host_id: 'zapp', port: 631, status: 'failed', updated_at: ago(7200),
      catalog: { scope: 'system', scope_reason: 'Confirmed as systemd unit cups-browsed.service', label: 'System daemon', purpose: 'Printer discovery', endpoint_scope: 'local_only' },
      lifecycle: { state: 'unavailable', last_seen_at: ago(7200), unavailable_since: ago(7000), reason: 'Unit entered failed state.' } }) },
    { state: 'current', ...mk({ service_id: 'svc-9', service_name: 'vite dev server', description: 'Dev server for a feature branch; nothing proves it was started from HerdR.', owner: 'node', host_id: 'beast', port: 5173, status: 'healthy', updated_at: ago(90),
      catalog: { scope: 'unassigned', scope_reason: 'Listener has no confirmed HerdR launch association', label: 'Dev server', purpose: 'Local preview', project: 'seylivo', endpoint_scope: 'local_only', metadata_observation: obs('observed', 40) },
      lifecycle: { state: 'current', last_seen_at: ago(90), reason: 'Status from the latest service report.' } }) },
    { state: 'unknown', ...mk({ service_id: 'svc-10', service_name: 'ngrok tunnel', description: 'Public tunnel to a local port.', owner: 'ngrok', host_id: 'bender', port: 4040, status: 'healthy', updated_at: ago(1800),
      catalog: { scope: 'unassigned', label: 'Tunnel', purpose: 'Share a local port', endpoint_scope: 'local_only', metadata_observation: obs('unconfirmed', 1800) },
      lifecycle: { state: 'current', last_seen_at: ago(1800), reason: 'Status from the latest service report.' } }) },
    { state: 'current', ...mk({ service_id: 'svc-11', service_name: 'jupyter-lab', description: 'Notebook server with a long description that wraps onto more than one line so the row shows how the page handles a verbose purpose.', owner: 'jupyter-lab', host_id: 'beast', port: 8888, status: 'running', updated_at: ago(70),
      catalog: { scope: 'unassigned', label: 'Notebook server', purpose: 'Exploratory analysis of mass-spec peak lists', project: 'everything-bagel', endpoint_scope: 'local_only', metadata_observation: obs('observed', 30) },
      lifecycle: { state: 'current', last_seen_at: ago(70), reason: 'Status from the latest service report.' } }) },
  ];
}

// The paged catalog answer for ?view=catalog (grouping, counts and facets as the server computes them).
function serviceCatalog(params, ago) {
  const all = serviceRecords(ago);
  const wanted = params.get('scope') || 'herdr';
  const status = params.get('status') || 'current';
  const includeArchived = params.get('include_archived') === 'true';
  const hostId = params.get('host_id') || '';
  const groupBy = params.get('group_by') || 'project';
  const terms = (params.get('search') || '').toLowerCase().split(/\s+/).filter(Boolean);
  const scoped = all.filter((s) => s.catalog.scope === wanted);
  const counts = { current: 0, unavailable: 0, unknown: 0, archived: 0 };
  const hosts = new Map();
  const groups = new Map();
  const selected = [];
  const typeLabel = (s) => s.catalog.label || 'Unclassified service';
  const group = (s) => {
    if (groupBy === 'host') return [`host:${s.host_id}`, s.host_id];
    if (groupBy === 'type') return [`type:${typeLabel(s)}`, typeLabel(s)];
    const project = s.catalog.project_id, repo = s.catalog.repository_id;
    const label = project || repo || `No verified project · ${typeLabel(s)}`;
    return [`${project ? 'project' : repo ? 'repository' : 'unassigned'}:${label}`, label];
  };
  for (const s of scoped) {
    counts[s.state]++;
    hosts.set(s.host_id, (hosts.get(s.host_id) || 0) + 1);
    const text = [s.service_name, s.description, s.owner, s.host_id, s.port, s.service_id, s.catalog.project, s.catalog.label, s.catalog.purpose].filter((x) => x != null).join(' ').toLowerCase();
    if ((s.state === 'archived' && !includeArchived) || (status !== 'all' && s.state !== status) || (hostId && s.host_id !== hostId) || !terms.every((t) => text.includes(t))) continue;
    const [key, label] = group(s);
    if (!groups.has(key)) groups.set(key, { key, label, total: 0, states: { current: 0, unavailable: 0, unknown: 0, archived: 0 } });
    groups.get(key).total++; groups.get(key).states[s.state]++;
    selected.push(s);
  }
  selected.sort((a, b) => group(a)[1].localeCompare(group(b)[1]) || a.service_name.localeCompare(b.service_name));
  const services = selected.map(({ state, ...rest }) => rest);
  return { services, as_of: ago(5), snapshot_id: 'snap-fixture', sort: 'group-name-id-nfc-codepoint-v1',
    counts: { ...counts, scope_total: scoped.length, history_total: scoped.length - counts.current, matching_total: selected.length, matching_groups: groups.size },
    facets: { hosts: [...hosts].sort(([a], [b]) => a.localeCompare(b)).map(([value, count]) => ({ value, count })) }, groups: [...groups.values()],
    page: { limit: Number(params.get('limit')) || 100, returned: services.length, has_more: false, next_cursor: null } };
}

const fabricServices = (ago) => ({ available: true, leases: [
  { id: 'lease-1', label: 'herdr-dashboard', url: 'https://dash.fabric.example', state: 'active', renewed_at: ago(30), target: { host: '100.64.12.4', port: 4173, machine_id: 'beast' }, owner: { herdr_session_id: 's-101', repository: 'org/repo' } },
  { id: 'lease-2', label: 'gateway-preview', url: 'https://gateway-preview.fabric.example', state: 'active', renewed_at: ago(75), target: { host: '100.64.12.9', port: 8088, machine_id: 'bender' }, owner: { herdr_session_id: 's-103' } },
  { id: 'lease-3', label: 'benchmark-report', url: 'https://bench.fabric.example', state: 'active', health_last_error: 'upstream returned 502 on the last 3 health checks', renewed_at: ago(310), target: { host: '100.64.12.9', port: 9100, machine_id: 'bender' }, owner: { repository: 'berlinguyinca/everything-bagel' } },
  { id: 'lease-4', label: null, url: 'https://notebook.fabric.example', state: 'active', renewed_at: ago(55), target: { host: '100.64.12.4', port: 8888, machine_id: 'beast' }, owner: {} },
  { id: 'lease-5', label: 'docs-preview', url: 'https://docs-preview.fabric.example', state: 'expired', renewed_at: ago(86400 * 2), target: { host: '100.64.12.21', port: 4000, machine_id: 'zapp' }, owner: { herdr_session_id: 's-102' } },
] });

// ---- LLM usage ----------------------------------------------------------------------------------------------------------------------------
const WINDOWS = { '1h': { secs: 3600, step: 300 }, '24h': { secs: 86400, step: 3600 }, '7d': { secs: 7 * 86400, step: 3600 }, '30d': { secs: 30 * 86400, step: 86400 } };
const MODELS = [
  { key: 'claude-sonnet-4-5', share: 0.46, in: 1.0, out: 1.0, ttft: [820, 1900], speed: [62, 88], errors: 0.021, price: { input: 3, output: 15, cached: 0.3 }, match: 'claude-sonnet-*' },
  { key: 'qwen3-35b-a3b', share: 0.27, in: 0.7, out: 1.3, ttft: [310, 760], speed: [118, 151], errors: 0.012, price: { input: 0.2, output: 0.8, cached: 0.05 }, match: 'qwen3-*' },
  { key: 'claude-opus-4-1', share: 0.17, in: 1.4, out: 0.8, ttft: [1450, 3600], speed: [34, 49], errors: 0.043, price: { input: 15, output: 75, cached: 1.5 }, match: 'claude-opus-*' },
  { key: 'gpt-5-codex', share: 0.10, in: 1.1, out: 0.9, ttft: [940, 2300], speed: [71, 96], errors: 0.034, price: { input: 1.25, output: 10, cached: 0.125 }, match: 'gpt-5-*' },
];
const PROJECTS = [{ key: 'herdr-engineering', share: 0.52 }, { key: 'everything-bagel', share: 0.24 }, { key: 'gateway', share: 0.15 }, { key: 'seylivo', share: 0.09 }];
const MISSION_KEYS = [{ key: 'pi:M-201', share: 0.33 }, { key: 'M-301', share: 0.27 }, { key: 'pi:M-202', share: 0.2 }, { key: 'M-302', share: 0.12 }, { key: 'M-303', share: 0.08 }];
const SESSIONS = [{ key: 's-101', share: 0.34, model: 0 }, { key: 's-103', share: 0.26, model: 1 }, { key: 's-102', share: 0.21, model: 2 }, { key: 's-104', share: 0.13, model: 3 }, { key: 'unmatched:s-legacy-77', share: 0.06, model: 0 }];

// How big a filtered slice is relative to the whole fleet.
function slice(params) {
  let f = 1;
  const project = PROJECTS.find((p) => p.key === params.get('project'));
  const model = MODELS.find((m) => m.key === params.get('model'));
  const mission = MISSION_KEYS.find((m) => m.key === params.get('mission'));
  const session = SESSIONS.find((s) => s.key === params.get('session'));
  if (project) f *= project.share; if (model) f *= model.share; if (mission) f *= mission.share; if (session) f *= session.share;
  return f;
}

// The requests-per-bucket series for a window: a working-hours wave over a baseline. Every other series derives from it.
function seriesBuckets(win, f) {
  const { secs, step } = WINDOWS[win] || WINDOWS['24h'];
  const n = Math.round(secs / step);
  return Array.from({ length: n }, (_, i) => {
    const day = win === '1h' ? 0.6 + 0.4 * Math.sin(i / 2.2) : 0.55 + 0.45 * Math.sin((i * step) / 86400 * 2 * Math.PI - 1.2);
    const lull = win === '7d' && Math.floor(i / 24) % 7 >= 5 ? 0.35 : 1;
    const requests = Math.max(win === '1h' ? 2 : 0, Math.round((win === '1h' ? 38 : win === '30d' ? 900 : 46) * (0.35 + day) * lull * (0.75 + 0.5 * wob(i, 5)) * f));
    const failed = Math.round(requests * (0.015 + 0.05 * wob(i, 9) * (i % 11 === 7 ? 4 : 1)));
    return { i, requests, failed: Math.min(failed, requests), t: n - 1 - i };   // t: bucket age in steps
  });
}

function timeseries(params, ago) {
  const win = params.get('window') || '24h';
  const { step } = WINDOWS[win] || WINDOWS['24h'];
  const metric = params.get('metric') || 'requests';
  const f = slice(params);
  const buckets = seriesBuckets(win, f);
  const point = (b, v) => ({ t: ago(b.t * step), v });
  const insufficient = (b) => b.requests < 6;   // under 5 samples a percentile is withheld, so the line breaks
  let points;
  if (metric === 'requests') points = buckets.map((b) => point(b, b.requests));
  else if (metric === 'errors') points = buckets.map((b) => point(b, b.failed));
  else if (metric === 'ttft_p95') points = buckets.map((b) => point(b, insufficient(b) ? null : Math.round(1500 + 900 * Math.sin(b.i / 5) + 700 * wob(b.i, 11))));
  else if (metric === 'speed_p50') points = buckets.map((b) => point(b, insufficient(b) ? null : Math.round((74 + 22 * Math.sin(b.i / 6 + 1) + 12 * wob(b.i, 13)) * 10) / 10));
  else if (metric === 'queue_p95') points = buckets.map((b) => point(b, insufficient(b) ? null : Math.round(120 + 260 * Math.max(0, Math.sin(b.i / 4)) + 90 * wob(b.i, 17))));
  else points = [];
  return { metric, step: `${step === 300 ? '5m' : step === 3600 ? '1h' : '1d'}`, window: win, points };
}

const dist = (n, p50, p95) => (n < 5 ? { n, insufficient: true } : { n, p50, p95 });

function statGroup(base, key, source, share, win, f, opts = {}) {
  const requests = Math.round(base * share * f);
  const failed = Math.round(requests * (opts.errors ?? 0.026));
  const out = Math.round(requests * (opts.out ?? 1900));
  const inp = Math.round(requests * (opts.in ?? 7400));
  const cache = Math.round(inp * (opts.cache ?? 3.1));
  const n = Math.max(0, requests - failed);
  const ttft = opts.ttft || [900, 2100], speed = opts.speed || [70, 94];
  return { key, source, requests, errors: failed, error_rate: requests ? Math.round((failed / requests) * 1000) / 1000 : 0,
    tokens: { input: inp, output: out, cache_read: cache, ...(opts.lowerBound ? { lower_bound: true } : {}) },
    ttft_ms: dist(Math.round(n * 0.8), ttft[0], ttft[1]), speed_tps: dist(Math.round(n * 0.7), speed[0], speed[1]), queue_ms: dist(Math.round(n * 0.5), 140, 410),
    last_seen: ago0(opts.lastSeen ?? 90) };
}
let ago0 = (s) => s;   // rebound per call so group rows can stamp last_seen without threading ctx through every helper

function stats(params, ago) {
  ago0 = ago;
  const win = params.get('window') || '24h';
  const group = params.get('group') || 'all';
  const previous = params.get('previous') === '1';
  const f = slice(params) * (previous ? 0.82 : 1);
  const totalRequests = seriesBuckets(win, 1).reduce((t, b) => t + b.requests, 0);
  const { secs } = WINDOWS[win] || WINDOWS['24h'];
  const meta = { window: win, since: ago(secs), retained_since: ago(86400 * 21), partial: secs > 86400 * 21, generated_at: ago(2) };
  const proxy = (key, share, over = {}) => statGroup(totalRequests, key, 'llm-proxy', share, win, f, over);
  let groups;
  if (group === 'all') {
    const main = proxy('all', 1, { errors: 0.027, lowerBound: true });
    // The transcript cross-check carries usage events and tokens only; it is shown but never added to the proxy figures.
    const transcript = { key: 'all', source: 'transcript', cross_check: true, events: Math.round(main.requests * 0.93), tokens: { input: Math.round(main.tokens.input * 0.97), output: Math.round(main.tokens.output * 0.96), cache_read: Math.round(main.tokens.cache_read * 0.95) }, last_seen: ago(150) };
    groups = [main, transcript];
  } else if (group === 'model') {
    groups = MODELS.map((m, i) => proxy(m.key, m.share, { errors: m.errors, ttft: m.ttft, speed: m.speed, in: 7400 * m.in, out: 1900 * m.out, lastSeen: 60 + i * 700 }));
    groups.push({ key: 'claude-sonnet-4-5', source: 'transcript', cross_check: true, events: Math.round(totalRequests * 0.43 * f), tokens: { input: Math.round(totalRequests * 0.43 * f * 7100), output: Math.round(totalRequests * 0.43 * f * 1800), cache_read: 0 }, last_seen: ago(300) });
  } else if (group === 'session') {
    groups = SESSIONS.map((s, i) => proxy(s.key, s.share, { errors: s.key.startsWith('unmatched') ? 0.11 : 0.02 + i * 0.006, speed: MODELS[s.model].speed, ttft: MODELS[s.model].ttft, lastSeen: 40 + i * 1500 }));
    groups.push({ key: 's-102', source: 'transcript', events: 214, tokens: { input: 1_480_000, output: 392_000, cache_read: 0 }, partial_source: true, last_seen: ago(900) });
  } else if (group === 'project') {
    groups = PROJECTS.map((p, i) => proxy(p.key, p.share, { lastSeen: 80 + i * 900 }));
  } else if (group === 'mission') {
    groups = MISSION_KEYS.map((m, i) => proxy(m.key, m.share, { lastSeen: 100 + i * 2500 }));
  } else groups = [];
  return { ...meta, groups };
}

function llmErrors(params, ago) {
  const f = Math.max(0.15, slice(params));
  const row = (cls, count, model, age, sessions) => ({ class: cls, count: Math.max(1, Math.round(count * f)), model, last_seen: ago(age), sessions, session_count: sessions.length });
  return { window: params.get('window') || '24h', problems: [
    row('upstream_timeout', 41, 'claude-opus-4-1', 420, ['s-102', 's-101']),
    row('rate_limited', 33, 'claude-sonnet-4-5', 1260, ['s-101', 's-103', 's-102', 's-104', 'unmatched:s-legacy-77', 's-105', 's-106']),
    row('context_length_exceeded', 12, 'gpt-5-codex', 5400, ['s-104']),
    row('stream_interrupted', 9, 'qwen3-35b-a3b', 9800, ['s-103']),
    row('invalid_tool_arguments', 4, 'claude-sonnet-4-5', 30000, ['unmatched:s-legacy-77']),
  ] };
}

function llmSpend(params, ago) {
  const win = params.get('window') || '24h';
  const f = slice(params);
  const days = Math.min(7, Math.max(1, Math.round((WINDOWS[win] || WINDOWS['24h']).secs / 86400)));
  const total = seriesBuckets(win, 1).reduce((t, b) => t + b.requests, 0);
  const tok = (m, share) => ({ input: Math.round(total * share * f * 7400 * m.in), output: Math.round(total * share * f * 1900 * m.out), cache_read: Math.round(total * share * f * 22000) });
  const cost = (t, p) => Math.round(((t.input * p.input + t.output * p.output + t.cache_read * p.cached) / 1e6) * 100) / 100;
  const byModel = MODELS.map((m) => { const tokens = tok(m, m.share); return { model: m.key, match: m.match, tokens, cost: cost(tokens, m.price) }; });
  const unpriced = [{ model: 'mistral-small-3', tokens: { input: Math.round(total * f * 210), output: Math.round(total * f * 60), cache_read: 0 } }];
  const dayRows = Array.from({ length: days }, (_, d) => {
    const dayShare = (0.7 + 0.6 * wob(d, 21)) / days;
    return { day: ago((days - 1 - d) * 86400).slice(0, 10), models: [...MODELS.map((m) => {
      const tokens = { input: Math.round(tok(m, m.share).input * dayShare), output: Math.round(tok(m, m.share).output * dayShare), cache_read: Math.round(tok(m, m.share).cache_read * dayShare) };
      return { model: m.key, tokens, cost: cost(tokens, m.price) };
    }), { model: 'mistral-small-3', tokens: { input: Math.round(unpriced[0].tokens.input * dayShare), output: Math.round(unpriced[0].tokens.output * dayShare), cache_read: 0 }, cost: null }] };
  });
  return { configured: true, source: 'llm-proxy', partial: (WINDOWS[win] || WINDOWS['24h']).secs > 86400 * 21,
    total: Math.round(byModel.reduce((t, m) => t + m.cost, 0) * 100) / 100, by_model: byModel, unpriced, days: dayRows };
}

// ---- analytics ----------------------------------------------------------------------------------------------------------------------------
const analytics = (kind) => {
  if (kind === 'missions') return { total: 38, completed: 24, completion_rate: 63, by_stage: { planning: 3, implementation: 7, verification: 4, review: 3, done: 21 } };
  if (kind === 'models') return { total_sessions: 57, models: { 'claude-sonnet-4-5': { sessions: 26, messages: 4180 }, 'qwen3-35b-a3b': { sessions: 15, messages: 2962 }, 'claude-opus-4-1': { sessions: 9, messages: 877 }, 'gpt-5-codex': { sessions: 7, messages: 640 } } };
  if (kind === 'hosts') return { total: 4, healthy: 2, unavailable: 2 };
  if (kind === 'tokens') {
    const u = (requests, i, o, r, c, failed) => ({ requests, input_tokens: i, output_tokens: o, reasoning_tokens: r, cache_write_tokens: c, failed });
    return { total: u(9412, 70_410_000, 17_872_000, 3_204_000, 9_850_000, 241),
      by_project: { 'herdr-engineering': u(4890, 36_700_000, 9_300_000, 1_700_000, 5_200_000, 112), 'everything-bagel': u(2260, 16_900_000, 4_300_000, 780_000, 2_410_000, 64), gateway: u(1410, 10_600_000, 2_680_000, 480_000, 1_480_000, 41), seylivo: u(852, 6_210_000, 1_592_000, 244_000, 760_000, 24) },
      by_model: { 'claude-sonnet-4-5': u(4330, 32_500_000, 8_240_000, 1_900_000, 5_000_000, 91), 'qwen3-35b-a3b': u(2540, 19_000_000, 4_820_000, 640_000, 2_100_000, 30), 'claude-opus-4-1': u(1600, 11_900_000, 3_020_000, 540_000, 1_700_000, 69), 'gpt-5-codex': u(942, 7_010_000, 1_792_000, 124_000, 1_050_000, 51) },
      by_day: { '2026-10-21': u(3020, 22_600_000, 5_700_000, 980_000, 3_100_000, 80), '2026-10-22': u(3394, 25_400_000, 6_480_000, 1_190_000, 3_600_000, 92), '2026-10-23': u(2998, 22_410_000, 5_692_000, 1_034_000, 3_150_000, 69) } };
  }
  if (kind === 'stages') return { bottleneck: 'verification', stages: { planning: { p50_ms: 184_000, p95_ms: 612_000, runs: 38 }, implementation: { p50_ms: 2_460_000, p95_ms: 7_980_000, runs: 34 }, verification: { p50_ms: 1_820_000, p95_ms: 9_420_000, runs: 29 }, review: { p50_ms: 540_000, p95_ms: 2_110_000, runs: 21 } },
    in_progress: { 'M-301': ['implementation'], 'pi:M-201': ['verification'], 'pi:M-202': ['implementation', 'review'] } };
  if (kind === 'usage') return { tokens: { total: { input_tokens: 70_410_000, output_tokens: 17_872_000, reasoning_tokens: 3_204_000 },
    by_model: { 'claude-sonnet-4-5': { input_tokens: 32_500_000, output_tokens: 8_240_000, reasoning_tokens: 1_900_000 }, 'qwen3-35b-a3b': { input_tokens: 19_000_000, output_tokens: 4_820_000, reasoning_tokens: 640_000 }, 'claude-opus-4-1': { input_tokens: 11_900_000, output_tokens: 3_020_000, reasoning_tokens: 540_000 }, 'gpt-5-codex': { input_tokens: 7_010_000, output_tokens: 1_792_000, reasoning_tokens: 124_000 } } },
    resources: { cpu_seconds: { count: 1840, total: 912_450, unit: 's' }, gpu_seconds: { count: 612, total: 188_300, unit: 's' }, ci_minutes: { count: 233, total: 4_870, unit: 'min' }, storage_gib_days: { count: 41, total: 1_204, unit: 'GiB-day' } } };
  return undefined;
};

// ---- dispatch -----------------------------------------------------------------------------------------------------------------------------
export function answer(pathname, method, params, ctx) {
  if (method !== 'GET') return undefined;
  const { ago } = ctx;
  let m;
  if (pathname === '/api/v1/hosts') return { hosts: hostRecords(ago) };
  if ((m = pathname.match(/^\/api\/v1\/hosts\/([^/]+)\/telemetry$/))) return HOST_NAMES[m[1]] ? telemetry(m[1], params.get('range') || '1h', ago) : undefined;
  if ((m = pathname.match(/^\/api\/v1\/hosts\/([^/]+)$/))) return hostRecords(ago).find((h) => h.host_id === m[1]);
  if (pathname === '/api/v1/fleet/inventory') return fleetInventory(ago);
  if (pathname === '/api/v1/fleet/config/status') return fleetConfigStatus();
  if (pathname === '/api/v1/scheduler') return scheduler();
  if (pathname === '/api/v1/ai-sync') return aiSync(ago);
  if (pathname === '/api/v1/services') return params.get('view') === 'catalog' ? serviceCatalog(params, ago) : { services: serviceRecords(ago).map(({ state, ...s }) => s) };
  if ((m = pathname.match(/^\/api\/v1\/services\/([^/]+)$/))) {
    const found = serviceRecords(ago).find((s) => s.service_id === decodeURIComponent(m[1]));
    if (!found) return undefined;
    const { state, ...service } = found;
    return service;
  }
  if (pathname === '/api/v1/fabric/services') return fabricServices(ago);
  if (pathname === '/api/v1/llm/stats') return stats(params, ago);
  if (pathname === '/api/v1/llm/timeseries') return timeseries(params, ago);
  if (pathname === '/api/v1/llm/errors') return llmErrors(params, ago);
  if (pathname === '/api/v1/llm/spend') return llmSpend(params, ago);
  if ((m = pathname.match(/^\/api\/v1\/analytics\/([^/]+)$/))) return analytics(m[1]);
  return undefined;
}

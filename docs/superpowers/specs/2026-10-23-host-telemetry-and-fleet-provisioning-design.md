# Host telemetry page and Ansible fleet provisioning — design

Status: draft for approval. Date: 2026-10-23.

## Intent

The Hosts page (`#/hosts/<id>`, `viewHost` in `packages/app.js`) is nearly
empty: for `beast` it shows `null` for the Tailnet IP and `—` for CPU, memory
and disk. The operator wants to open a host and immediately see whether it is
healthy: outdated packages, CPU, uptime, problems, memory, running agent
count and top consumers, with history over time. They also want GPU and disk.

Second, they want Ansible to provision the whole fleet from scratch (install
Tailscale and join the tailnet, install herdr, pi, the pi extensions and agent
tools) and then keep it all updated, so a host can be reinstalled without
ad-hoc work.

## Findings that shape the design (verified 2026-10-23 on origin/main)

- Control plane already has `POST /api/v1/hosts/{id}/telemetry`
  (`control_plane/api.py`), `repo.record_telemetry`, `repo.list_telemetry`, and
  a generic host event path (`ingest.py::_ingest_host`) that accepts
  `cpu`, `mem`, `disk`. **No real host ever sends them**; only demo seed data
  (`cli.py`) does. This is why the page is empty.
- Samples are held in memory, capped at 500 per host, lost on restart.
- `viewHost` renders `h.tailnet_ip` with no fallback (the `null`).
- `fabric_agent.py` already runs on every fleet host under systemd and already
  reports to the control plane. It is the natural carrier for a collector.
- `ansible/` already has `herdr_linux`, `herdr_macos`, and tasks for the fabric
  agent, systemd units, pi runtime, pi build-gate and web UI. Tailscale is only
  checked, never installed or joined. Nothing schedules updates.

## Goals

1. Hosts detail page shows, per host: health badge, uptime, running agent
   count, last-report age; CPU, memory, GPU (util, VRAM, temp) and disk, each
   with current value and history (1h / 24h / 7d); top consumers; outdated
   packages; a Problems list that is empty when the host is fine.
2. Hosts list cards show the same headline numbers and a problem count.
3. Real telemetry from every fleet host, durable across control-plane restart.
4. Ansible provisions a fresh host end to end and converges updates on a timer.

## Non-goals

- No replacement for Prometheus/Grafana; no alerting/paging, no per-process
  history, no metrics for arbitrary services.
- No new runtime dependencies on hosts (stdlib Python only; `psutil` is not
  assumed present).
- No Windows hosts.
- Not changing the existing 500-sample in-memory API contract for current
  callers; history is additive.

## Anti-goals

- A page that renders green because data is missing. Missing/stale telemetry
  must show as a problem ("no report for 12 min"), never as "Healthy".
- Updates that silently restart agents mid-mission, or an auth key committed
  to Git.

## Design

### 1. Collector (in `fabric_agent.py`, new module `host_metrics.py`)

Runs on a thread in the existing agent. Linux reads `/proc` and `statvfs`;
macOS uses `vm_stat`/`sysctl`/`df`/`ps`. Each degrades to "field absent",
never to an exception.

Fast sample every 30 s: `cpu` (percent from `/proc/stat` deltas), `load1/5/15`,
`mem` (percent, plus used/total bytes), `swap`, `disk` (percent for the
fleet-state mount plus a list of mounts), `uptime_s`, `net_rx/tx` rates, GPU
list from `nvidia-smi --query-gpu` or `rocm-smi --json` (util, vram used/total,
temp, name; absent if no tool), `agents_running` (count of active pi/herdr
sessions known to the agent), and `top`: five processes by CPU and five by
memory (pid, command, user, percent, and the agent/service name when the pid
maps to a known session or unit).

Slow report every 60 min (and once at agent start): `updates` — count of
outdated OS packages and count flagged security (apt: `apt-get -s upgrade`
parse; brew: `brew outdated`), reboot-required flag, failed systemd units, and
`versions` — herdr, pi, pi extensions, autospec, tailscale.

Sent as the existing host event with a `telemetry` object; `_ingest_host`
extends to store it. Payload size is bounded (top lists truncated, mounts
capped).

### 2. Control plane

- Persist samples to the existing durable event sink / a table (builder picks
  the fit with current persistence) with downsampling: raw 30 s for 48 h,
  5-minute rollups for 7 days, then dropped. History API:
  `GET /api/v1/hosts/{id}/telemetry?range=1h|24h|7d` returns a bounded series
  (max ~300 points).
- Latest `updates`/`versions`/`top` kept on the host record.
- `problems` computed server-side so the list view and detail view agree:
  stale (no report > 3 intervals), disk > 85% warn / > 95% critical, memory
  > 90% sustained 5 min, swap thrash, GPU temp > 85 °C, failed units, security
  updates pending, reboot required, version drift vs fleet majority.
  Thresholds are config values with these defaults. Each problem has severity
  and a short human message.
- `tailnet_ip` null is fixed at the source (collector sends it) and the UI
  shows "not reported" instead of `null`.

### 3. UI (`viewHost`, `viewHosts`)

Detail page top to bottom: header (name, health, uptime, agents running,
last report); Problems strip (hidden when empty); gauge row CPU / Memory / GPU
/ Disk, each a value plus an SVG sparkline with a range picker; Top consumers
(two small tables); Updates (counts, expandable list, versions table). Plain
SVG, no chart library, using the existing `herdr-*` components and CSS tokens.
List cards gain CPU/mem/GPU bars and a problem badge. Stale telemetry renders
the badge as stalled (existing `reportFreshness`).

### 4. Ansible

New roles under `ansible/roles/`, called from `playbooks/site.yml`:

- `tailscale`: install from the official repo (apt/brew), enable `tailscaled`,
  `tailscale up --auth-key=… --hostname={{ inventory_hostname }}` only when the
  node is not already logged in (idempotent via `tailscale status --json`).
  **Auth key is supplied at run time** from Ansible Vault or `--extra-vars`
  from a prompt; it is never in Git or host_vars in clear. Recommended key
  type: reusable, pre-authorized, tagged (e.g. `tag:fleet`), 90-day expiry.
  (This was not specified by the operator; it is the builder's call and is
  flagged for review.)
- `herdr_fleet_update`: a systemd timer (launchd on macOS) that updates OS
  packages (security-only by default; full upgrade opt-in), then herdr, pi, pi
  extensions and autospec to the pinned/latest versions defined in
  `group_vars`, and restarts the fabric agent. It defers when agents are
  running a mission unless `force` is set, and reports its result in the
  `versions` telemetry.
- Existing roles continue to own herdr/pi install; `fleet-status.yml` is
  extended to print the new telemetry for a host over SSH as a fallback.
- Playbook order: baseline, tailscale, herdr/pi, fabric agent + collector,
  update timer. Re-running on a configured host changes nothing.

## Order of work

1. Collector + ingest (data starts flowing; fixes the empty page's cause).
2. Control-plane persistence, history API, problems.
3. UI.
4. Ansible roles and update timer.
Each step is independently shippable and tested.

## Testing

Collector: unit tests parsing recorded `/proc`, `nvidia-smi`, `apt` fixtures,
including missing-tool and malformed cases. Control plane: downsampling and
problems tables, stale-data case. UI: existing `packages` test style for
rendering with/without telemetry, GPU absent, many problems. Ansible:
`ansible-lint`, `--syntax-check`, and a check-mode run against `beast`;
idempotency is a second run with zero changes.

## Left to the builder

Storage mechanism for history, exact sparkline styling, apt/brew parsing
details, launchd vs cron for macOS, how the collector discovers which pids are
agents, and package-manager coverage beyond apt/brew.

## Calls made without the operator

- Tailnet join uses a runtime-supplied reusable tagged auth key (above).
- Updates are security-only by default and defer while a mission runs.
- Retention 48 h raw / 7 d rollup; thresholds listed above.
- Ansible work extends the existing `ansible/` tree rather than the separate
  `fry-ansible` / `cubert-ansible` repos.

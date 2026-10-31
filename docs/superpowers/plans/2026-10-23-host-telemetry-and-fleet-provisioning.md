# Host Telemetry Page and Fleet Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Real per-host telemetry (CPU, memory, GPU, disk, uptime, agents, top consumers, outdated packages, problems) with history on the Hosts pages, and Ansible that provisions (Tailscale join, herdr, pi) and keeps the fleet updated from the main host.

**Architecture:** A stdlib-only collector (`host_metrics.py`) runs on a thread inside the existing fabric agent and posts a `host` event with a `telemetry` object to `/api/v1/ingest` every 30 s (slow package/version data hourly). `ingest._ingest_host` hands it to a new `repo.record_host_metrics`, which keeps history with rollups and feeds a server-side `compute_problems`. A new `packages/host-view.js` renders the page. Ansible gets a `tailscale` role and a `herdr_fleet_update` role run by cron on the gateway host.

**Tech Stack:** Python 3 stdlib (pytest), vanilla ES modules (`node --test`), Ansible.

**Spec:** `docs/superpowers/specs/2026-10-23-host-telemetry-and-fleet-provisioning-design.md`

## Global Constraints

- Collector: stdlib only, no `psutil`; sample every 30 s; slow report (updates, versions) every 60 min and once at agent start.
- A missing tool or unreadable file omits the field; it never raises and never shows as zero.
- Top lists: 5 by CPU, 5 by memory. Mounts capped at 8, GPUs at 8, payload under 16 KiB.
- History: raw 30 s samples kept 48 h, 5-minute rollups kept 7 d; range API returns at most 300 points (`range=1h|24h|7d`).
- Staleness reuses `REPORT_FRESH_MS` (15 min, `packages/record-state.js`).
- Problem thresholds (config defaults): disk warn 85 % / critical 95 %; memory > 90 % sustained 5 min; GPU temp > 85 °C; updates last run older than 14 d; update deferral maximum 7 d.
- Missing/stale telemetry is a Problem, never "Healthy".
- No auth key, token or secret in Git or logs; Tailscale key comes from Ansible Vault on the main host (`tailscale_auth_key`), tag `tag:fleet`.
- Updates: security-only OS updates by default; pushed from the gateway host (`herdr_engineering_gateway_host`, `bender`) by cron; no `ansible-pull`; a host with running agents is deferred up to 7 d.
- macOS is a first-class target (Apple GPU: name, cores, unified-memory use; utilization only with passwordless sudo).
- Hosts with `maintenance: true` in inventory skip the stale problem.
- Follow existing layout: Python tests in `tests/` (control plane in `tests/control_plane/`), JS tests beside components in `packages/herdr-web-components/*.test.js`, run with `python -m pytest` and `npm run test:client`.

## Review Focus

1. Host that has never reported: page says "No telemetry reported" and raises the stale problem, not a green badge or `null`.
2. Host without a GPU or without `nvidia-smi`/`rocm-smi`: GPU section absent, no zeros, no error.
3. `nvidia-smi` or `apt-get` hangs: sample still posts on time with that field omitted (5 s command timeout).
4. Oversized, NaN or wrongly typed telemetry values (including strings in numeric fields, 10 000-entry `top`): clamped or dropped, other fields kept, no 500.
5. Control-plane restart / empty history / host names containing HTML: sparkline renders empty gracefully, names are escaped.

---

### Task 1: Core sampler

**Files:**
- Create: `herdr_engineering/host_metrics.py`
- Test: `tests/test_host_metrics.py`, fixtures under `tests/fixtures/host_metrics/`

**Interfaces:**
- Produces: `class HostSampler` with `sample() -> dict`; keys `cpu`, `load1`, `load5`, `load15`, `mem`, `mem_used_b`, `mem_total_b`, `swap`, `disk`, `mounts` (`[{"path","pct","used_b","total_b"}]`), `uptime_s`, `net_rx_bps`, `net_tx_bps`. First call returns no `cpu`/`net_*` (needs a previous reading). Module-level `parse_proc_stat(text) -> tuple[int,int]` (busy, total), `parse_meminfo(text) -> dict`.

- [ ] **Step 1: Write failing tests** in `tests/test_host_metrics.py`: `test_cpu_percent_from_two_proc_stat_readings` (fixtures differing by 50 % busy → `cpu == 50.0`), `test_meminfo_percent_uses_memavailable` (total 16 GiB, avail 4 GiB → `mem == 75.0`), `test_missing_proc_files_omit_fields` (patch the reader to raise `OSError` → no keys, no exception), `test_macos_branch_uses_vm_stat_and_sysctl` (patch `platform.system` → `"Darwin"` and subprocess output fixtures → `mem` and `uptime_s` present).
- [ ] **Step 2: Run** `python -m pytest tests/test_host_metrics.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement `HostSampler`.** Linux reads `/proc/stat`, `/proc/meminfo`, `/proc/loadavg`, `/proc/uptime`, `/proc/net/dev` (sum non-`lo`), `os.statvfs` for mounts from `/proc/mounts` (real filesystems only, cap 8, `disk` = pct of the mount holding `~/.local/share/herdr-engineering`). macOS uses `vm_stat`, `sysctl -n vm.swapusage kern.boottime hw.memsize`, `df -k`, `netstat -ib`. Every reader wrapped so failure omits the field.
- [ ] **Step 4: Run tests** → PASS.
- [ ] **Step 5: Commit** `feat: host metrics core sampler`

### Task 2: GPU, top consumers, agent count

**Files:**
- Modify: `herdr_engineering/host_metrics.py`
- Test: `tests/test_host_metrics_gpu_top.py`

**Interfaces:**
- Produces: `parse_nvidia_smi(csv: str) -> list[dict]`, `parse_rocm_smi(json_text: str) -> list[dict]`, `parse_apple_gpu(system_profiler_json: str) -> list[dict]` each returning `{"name","util_pct"|None,"vram_used_b"|None,"vram_total_b"|None,"temp_c"|None}`; `parse_ps(output: str, limit: int = 5) -> dict` → `{"cpu":[...],"mem":[...]}` entries `{"pid","command","user","cpu_pct","mem_pct","owner"}`; `HostSampler.__init__(self, agent_lookup: Callable[[int], str|None] = lambda pid: None, agents_running: Callable[[], int] = lambda: 0)`; `sample()` adds `gpus`, `top`, `agents_running`.

- [ ] **Step 1: Failing tests:** nvidia fixture with two cards → two dicts with `temp_c`; rocm JSON fixture; Apple fixture gives `name` and no `util_pct` when sudo unavailable; `test_parse_ps_top_five_by_cpu_and_mem`; `test_owner_comes_from_agent_lookup` (lookup returns `"pi:mission-1"` for pid 42); `test_gpu_tool_missing_omits_key`; `test_gpu_command_timeout_omits_key_and_sample_still_returns` (patch the runner to raise `subprocess.TimeoutExpired`); `test_huge_ps_output_is_capped_at_five`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the parsers and wire them in; all subprocesses use a shared `_run(cmd, timeout=5.0) -> str | None` helper that returns `None` on any failure. Apple utilization via `sudo -n powermetrics` only when `sudo -n true` succeeds.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: GPU, top consumers and agent count in host metrics`

### Task 3: Slow report (updates, failed units, versions)

**Files:**
- Modify: `herdr_engineering/host_metrics.py`
- Test: `tests/test_host_metrics_updates.py`

**Interfaces:**
- Produces: `read_updates() -> dict` = `{"manager": "apt"|"brew"|None, "outdated": int|None, "security": int|None, "packages": [{"name","from","to"}] (cap 50), "reboot_required": bool|None, "failed_units": [str]}`; `parse_apt_simulate(text: str) -> dict`, `parse_brew_outdated(text: str) -> dict`; `read_versions() -> dict[str, str|None]` with keys `herdr`, `pi`, `pi_extensions` (dict name→version), `autospec`, `tailscale`.

- [ ] **Step 1: Failing tests:** apt `Inst` lines incl. `-security` suite → `outdated == 3`, `security == 1`; `/var/run/reboot-required` present → `reboot_required is True`; unknown package manager → `outdated is None` and `manager is None` (so the UI shows "unknown", not 0); `systemctl --failed` fixture → `failed_units == ["foo.service"]`; `test_versions_missing_tool_is_none`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** using `_run` (apt: `apt-get -s upgrade`; brew: `brew outdated --json=v2`; units: `systemctl --failed --no-legend`; versions via each tool's `--version`, extensions from the pi extensions directory manifest).
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat: package, unit and version report`

### Task 4: Agent integration

**Files:**
- Modify: `herdr_engineering/fabric_agent.py` (new `_report_host(telemetry: dict, include_slow: bool) -> bool` beside `_report_service`; thread started with the agent), `herdr_engineering/cli.py` (`_cmd_dev_agent`: pass `tailnet_ip`).
- Test: `tests/test_fabric_agent_host_report.py`

**Interfaces:**
- Consumes: `HostSampler`, `read_updates`, `read_versions` (Tasks 1-3).
- Produces: POST to `{control_plane_url}/api/v1/ingest` with `{"source":"fabric-agent","entity_type":"host","entity_id":<machine>,"event_type":"host.heartbeat","payload":{"host_name","tailnet_ip","cpu","mem","disk","telemetry":{...},"updates":{...}?, "versions":{...}?}}`. `AgentConfig` gains `host_report_interval: float = 30.0`, `host_slow_interval: float = 3600.0`.

- [ ] **Step 1: Failing tests:** with a fake HTTP server, `test_agent_posts_host_event_with_telemetry`; `test_slow_fields_only_on_first_and_hourly`; `test_control_plane_down_does_not_raise` (mirrors `_report_service`'s best-effort contract); `test_no_control_plane_url_posts_nothing`; `test_tailnet_ip_none_is_sent_as_null_not_string`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `_report_host` with the same urllib/timeout pattern as `_report_service`; `agents_running` and `agent_lookup` come from the agent's known sessions. **Step 4: Run** → PASS. **Step 5: Commit** `feat: fabric agent reports host telemetry`

### Task 5: Control-plane storage and history

**Files:**
- Modify: `herdr_engineering/control_plane/ingest.py` (`_ingest_host`: forward `telemetry`, `updates`, `versions`, `pins`, `fleet_update` payload keys), `herdr_engineering/control_plane/repo.py` (alongside `record_telemetry`), `herdr_engineering/control_plane/api.py` (GET telemetry route near line 1145)
- Create: `herdr_engineering/control_plane/host_metrics_store.py`
- Test: `tests/control_plane/test_host_metrics_store.py`, `tests/control_plane/test_host_telemetry_api.py`

**Interfaces:**
- Produces: `clean_metrics(raw: dict) -> dict` (drops non-finite/non-numeric numeric fields, clamps percents to 0-100, caps lists, rejects payloads over 16 KiB with `ValueError`); `repo.record_host_metrics(host_id: str, sample: dict, *, updates: dict | None = None, versions: dict | None = None, pins: dict | None = None, fleet_update: dict | None = None) -> None` (also appends the legacy `cpu_pct`/`mem_pct`/`disk_pct` keys so `viewHost`'s old reads keep working); `repo.host_history(host_id: str, range: str) -> list[dict]` (≤300 points, rollup for 24h and 7d); `GET /api/v1/hosts/{id}/telemetry?range=1h|24h|7d` → `{"samples": [...], "range": "..."}`; the host record carries `latest`, `updates`, `versions`, `pins`, `fleet_update`. Existing `?limit=` behaviour and the 500-sample contract still work.

- [ ] **Step 1: Failing tests:** `test_history_range_returns_at_most_300_points`; `test_raw_samples_expire_after_48h_and_rollups_after_7d` (injectable clock); `test_history_survives_new_repo_over_same_sink` (durable path); `test_clean_metrics_clamps_and_drops_nan`; `test_oversized_payload_is_400_not_500`; `test_legacy_limit_param_unchanged`; `test_unknown_range_is_400`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** Persistence choice is the builder's (spec), but only rollups plus the latest record are written durably; raw samples stay in memory. **Step 4: Run** → PASS. **Step 5: Commit** `feat: durable host telemetry history`

### Task 6: Problems

**Files:**
- Create: `herdr_engineering/control_plane/host_problems.py`
- Modify: `repo.py` (`get_host`/`list_hosts` attach `problems` and `problem_count`), `config.py` (thresholds)
- Test: `tests/control_plane/test_host_problems.py`

**Interfaces:**
- Produces: `@dataclass class Thresholds` with the Global Constraints defaults; `compute_problems(host: dict, history: list[dict], now: float, thresholds: Thresholds = Thresholds()) -> list[dict]`, entries `{"code","severity":"warning"|"critical","message"}`; codes `stale`, `disk_warn`, `disk_critical`, `mem_pressure`, `gpu_hot`, `failed_units`, `security_updates`, `reboot_required`, `version_drift`, `updates_deferred`, `updates_old`, `updates_unknown`, `never_reported`.

- [ ] **Step 1: Failing tests** (one per code, table-driven): `never_reported` when no `latest`; `stale` at 16 min but not 14; `maintenance: true` suppresses `stale` only; `mem_pressure` needs 5 min of samples above 90 (a single spike does not); `version_drift` when `versions` differs from `pins` and not when `pins` is absent; `updates_deferred` after 7 d of `fleet_update.deferred_since`; `updates_unknown` when `updates.outdated is None`; healthy host → `[]`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** as a pure function. **Step 4: Run** → PASS. **Step 5: Commit** `feat: server-side host problems`

### Task 7: Host detail page

**Files:**
- Create: `packages/host-view.js`
- Modify: `packages/app.js` (`viewHost`, line ~365: fetch host, `/telemetry?range=`, render via `host-view.js`; range picker stored in the hash query)
- Test: `packages/herdr-web-components/host-view.test.js`

**Interfaces:**
- Produces: `renderHostDetail(host, history, { range, now }) -> string` (HTML, all dynamic text escaped), `sparkline(points: number[], { width, height, max }) -> string` (inline SVG; empty or one-point input returns an empty-state placeholder), `formatUptime(seconds) -> string` (`"3d 4h"`), `formatBytes(n) -> string`.

- [ ] **Step 1: Failing tests:** `test_never_reported_host_shows_no_telemetry_and_never_healthy` (asserts text "No telemetry reported", absence of "Healthy"); `test_tailnet_ip_null_shows_not_reported` (no literal `null`); `test_no_gpus_omits_gpu_section`; `test_problems_strip_hidden_when_empty`; `test_problems_listed_with_severity`; `test_sparkline_empty_and_single_point`; `test_host_name_is_escaped` (`<img onerror>` stays inert); `test_top_consumers_two_tables`; `test_updates_unknown_not_zero`; `formatUptime(273600) === "3d 4h"`.
- [ ] **Step 2: Run** `npm run test:client` → FAIL. **Step 3: Implement** with the existing `herdr-*` components and CSS tokens, no chart library. **Step 4: Run** → PASS; open the page against a seeded control plane and check it manually. **Step 5: Commit** `feat: host detail telemetry page`

### Task 8: Hosts list cards

**Files:** Modify `packages/app.js` (`viewHosts`, line ~342) and `packages/host-view.js` (`renderHostCard(host, { now }) -> string`); test `packages/herdr-web-components/host-view.test.js`.

- [ ] **Step 1: Failing tests:** card shows CPU/memory/GPU meters when `latest` has them and a problem badge when `problem_count > 0`; stale host renders the `stalled` stage (existing `reportFreshness`). **Step 2-4:** run, implement, run. **Step 5: Commit** `feat: telemetry on host cards`

### Task 9: Ansible Tailscale role

**Files:** Create `ansible/roles/tailscale/tasks/main.yml`, `ansible/roles/tailscale/defaults/main.yml`; modify `ansible/playbooks/site.yml` (include before the herdr role; replace the informational check) and `ansible/README.md`.

**Interfaces:** vars `tailscale_auth_key` (required only when not logged in; from Vault), `tailscale_tags: ["tag:fleet"]`, `tailscale_hostname: "{{ inventory_hostname }}"`.

- [ ] **Step 1: Write the check first:** `ansible-playbook --syntax-check` and `ansible-lint ansible/roles/tailscale` must fail on the missing role. **Step 2:** implement: install from the official repo (apt) or Homebrew cask (macOS); enable `tailscaled`; run `tailscale up --auth-key … --advertise-tags …` with `no_log: true` only when `tailscale status --json` reports `BackendState != "Running"`; a host already running is untouched; missing key on an unjoined host fails with a message that never prints the key. **Step 3:** `--check` run against `beast` shows no change when already joined; second real run reports 0 changed. **Step 4: Commit** `feat: ansible tailscale role`

### Task 10: Ansible fleet update from the main host

**Files:** Create `ansible/roles/herdr_fleet_update/{tasks,defaults,templates}`, `ansible/playbooks/fleet-update.yml`, `ansible/tasks/fleet-update-cron.yml`; modify `ansible/playbooks/site.yml` (cron installed on `herdr_engineering_gateway_host` only), `ansible/inventory/hosts.example.yml` (`maintenance` example), `ansible/README.md`.

**Interfaces:** vars `fleet_update_security_only: true`, `fleet_update_max_defer_days: 7`, `fleet_update_force: false`, `fleet_pins` (herdr, pi, pi_extensions, autospec versions in `group_vars/all.yml`). Per host the play: reads `agents_running` from `GET {{ fabric_agent_gateway_url }}/api/v1/hosts/<id>` → if > 0 and deferral < 7 d, records `deferred_since` in `~/.local/share/herdr-engineering/fleet-update-state.json` on the main host and skips; otherwise updates OS packages, herdr, pi, pi extensions, autospec, restarts the fabric agent; then POSTs a `host` ingest event with `fleet_update: {last_updated, deferred_since, result}` and `pins: {...}`. Cron entry: daily 03:30, `ansible-playbook playbooks/fleet-update.yml`.

- [ ] **Step 1: Failing check:** `--syntax-check` and a `--check` run of `fleet-update.yml` against `beast` fail before the role exists. **Step 2:** implement; Debian uses `apt` with `unattended-upgrade`-style security filter when `fleet_update_security_only`; macOS uses `brew upgrade`. **Step 3:** verify idempotency (second run: only the timestamp event changes) and the deferral path with a host reporting `agents_running: 1`. **Step 4: Commit** `feat: ansible fleet update from main host`

### Task 11: Documentation and end-to-end check

**Files:** Modify `ansible/README.md`, `README.md` (Hosts page + telemetry paragraph), `CHANGELOG.md`.

- [ ] **Step 1:** document the Vault key setup, the `tag:fleet` ACL prerequisite, the cron, and the problem codes. **Step 2:** run the full suites: `python -m pytest tests/test_host_metrics*.py tests/test_fabric_agent_host_report.py tests/control_plane -q` and `npm run test:client`; expected all pass. **Step 3:** CI runs full suites (see repo memory); push is left to the operator. **Step 4: Commit** `docs: host telemetry and fleet provisioning`

---

## Self-review notes

- Spec coverage: goals 1-2 → Tasks 7-8; goal 3 → Tasks 1-5; goal 4 → Tasks 9-10; problems → 6; Apple GPU → 2; pins-based drift and maintenance flag → 5, 6, 10; deferral → 10.
- One spec conflict resolved here: the spec says stale is "> 3 intervals" in one place and reuses `reportFreshness` in the decisions list; the plan uses the 15-minute `REPORT_FRESH_MS`, the operator-approved call.
- Rollout is the operator's: no deploy step is in this plan.

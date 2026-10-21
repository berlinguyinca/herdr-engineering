# Central Fleet Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One 0600 YAML file on the control-plane host holds all fleet configuration (secrets included); hosts fetch their role-scoped slice from an endpoint at startup; no configuration env vars remain.

**Architecture:** A pure module `herdr_engineering/fleet_config.py` loads/validates the file, computes a revision, authenticates per-host tokens, and returns a role-filtered slice. `api.py` serves it. A small client (`host_config.py` for Python, `host-config.js` for the plugin, a TS equivalent in pi-engineering) reads `host.json`, fetches, caches, and exposes typed values to consumers.

**Tech Stack:** Python 3 + PyYAML (already a dependency), Node ESM for the plugin, TypeScript for pi-engineering, pytest / `node --test`.

**Spec:** `docs/superpowers/specs/2026-10-21-central-fleet-config-design.md`

## Global Constraints

- File: `~/.config/herdr-engineering/fleet-config.yaml`, mode 0600, outside git; the repo holds only `deploy/control-plane/fleet-config.example.yaml` with `REDACTED` placeholders.
- Host bootstrap file: `~/.config/herdr-engineering/host.json`, mode 0600, keys `control_plane_url`, `host_id`, `token`.
- Endpoint: `GET /api/v1/fleet/config` (header `X-Host-Token`), `Cache-Control: no-store`; `POST /api/v1/fleet/config/applied` `{host_id, revision}`; `GET /api/v1/fleet/config/status` (no values).
- Host tokens are random; the file stores only `sha256` hex of a token. Unknown/revoked token: 401, body without detail.
- Secrets never appear in logs, errors, the Hosts page, or the status endpoint.
- No configuration env vars remain after slice 4 (decided 2026-10-21). Process inputs the platform sets (`HERDR_PANE_ID`, `XDG_RUNTIME_DIR`, `HOME`, `PATH`) are not configuration.
- An invalid edit is rejected; the last good revision keeps serving. Dated backups on each accepted change.
- Host with neither a fetch nor a cached copy refuses to start with a clear message.
- Never use `gh pr merge --delete-branch`. No secrets in commits.

## Review Focus

- Token for a host whose roles are empty: gets only the `all` sections, never another role's secrets.
- File edited to invalid YAML while the server runs: keeps serving the last good revision, status reports the error without values.
- Two hosts with the same token hash: load rejected.
- Control plane unreachable at plugin start: cached copy used; no cache: exits nonzero with message.
- A secret value containing `$`, quotes, or newlines survives `render-compose` into the compose env file.
- Revoked token still holding a cached config: next fetch gets 401 and the host stops using the cache after one failed refresh.

---

### Task 1: Config model, validation, revision, scoping

**Files:**
- Create: `herdr_engineering/fleet_config.py`
- Create: `deploy/control-plane/fleet-config.example.yaml`
- Test: `tests/test_fleet_config.py`

**Interfaces:**
- Produces:
  - `class FleetConfigError(ValueError)`
  - `def load_fleet_config(path: Path) -> FleetConfig` (raises `FleetConfigError`; refuses a file whose mode allows group/other access)
  - `FleetConfig.revision: str` (first 12 hex of sha256 over canonical JSON of sections+hosts)
  - `FleetConfig.host_for_token(token: str) -> str | None` (constant-time over sha256 hashes)
  - `FleetConfig.slice_for(host_id: str) -> dict` (merge of `all` sections and sections whose `roles` intersect the host's roles, then the host's `overrides`)
  - `FleetConfig.status() -> dict` (revision and host ids/roles, no values)

File shape: `version: 1`, `sections: {name: {roles: [..]|["all"], values: {..}}}`, `hosts: {id: {roles: [..], token_sha256: str, overrides: {..}}}`.

- [ ] **Step 1: Write failing tests** in `tests/test_fleet_config.py`: `test_slice_contains_only_allowed_sections`, `test_empty_roles_host_gets_only_all_sections`, `test_overrides_win`, `test_duplicate_token_hash_rejected`, `test_group_readable_file_rejected`, `test_revision_changes_with_content_not_comments`, `test_host_for_token_unknown_returns_none`, `test_status_has_no_values` (asserts a secret string is absent from `json.dumps(status())`).
- [ ] **Step 2: Run** `python3 -m pytest tests/test_fleet_config.py -q`; expect FAIL (module missing).
- [ ] **Step 3: Implement** the module per the interfaces; canonical JSON via `json.dumps(sort_keys=True)`; use `secrets.compare_digest` per stored hash.
- [ ] **Step 4: Run** the same command; expect PASS.
- [ ] **Step 5: Commit** `feat(fleet-config): model, validation, scoping`.

### Task 2: Server integration and endpoints

**Files:**
- Modify: `herdr_engineering/control_plane/api.py` (new routes near the other `/api/v1/fleet/*` routes; constructor arg `fleet_config: FleetConfigStore | None`)
- Modify: `herdr_engineering/fleet_config.py` (add `FleetConfigStore`)
- Modify: `herdr_engineering/cli.py` (`control-plane web --fleet-config PATH`, default `~/.config/herdr-engineering/fleet-config.yaml`)
- Test: `tests/control_plane/test_api.py`

**Interfaces:**
- Consumes: Task 1 `load_fleet_config`, `host_for_token`, `slice_for`, `status`.
- Produces: `class FleetConfigStore(path: Path)` with `current() -> FleetConfig`, `reload() -> bool` (keeps last good on failure; records `last_error: str | None` without values), `record_applied(host_id: str, revision: str) -> None`, `applied() -> dict[str, str]`. The store re-checks the file mtime on each request.

- [ ] **Step 1: Write failing tests**: `test_fleet_config_requires_valid_host_token` (401 without/with bad token, body has no detail), `test_fleet_config_returns_scoped_slice_no_store_header`, `test_applied_revision_shows_in_status`, `test_broken_edit_keeps_last_good_revision`, `test_status_never_contains_secret_values`. Endpoints bypass the operator `X-API-Key` gate by design (host token is their credential).
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_api.py -k fleet_config -q`; expect FAIL.
- [ ] **Step 3: Implement** store and routes; `GET` returns `{"revision": ..., "values": ...}`.
- [ ] **Step 4: Run** tests plus `python3 -m pytest tests/control_plane -q`; expect PASS.
- [ ] **Step 5: Commit** `feat(control-plane): serve role-scoped fleet config`.

### Task 3: `fleet join` credentials, `fleet config init|validate|token`

**Files:**
- Modify: `herdr_engineering/fleet.py` (`fleet_join` gains step "config": mint token, add host to the file, write `host.json` on the target over ssh, 0600)
- Create: `herdr_engineering/fleet_config_cli.py`
- Modify: `herdr_engineering/cli.py` (register `fleet config {init,validate,token,render-compose}`)
- Test: `tests/test_fleet_config_cli.py`

**Interfaces:**
- Consumes: Task 1 model.
- Produces: `def mint_host_token() -> tuple[str, str]` (token, sha256 hex); `def init_config(path: Path, answers: dict | None, prompt=input) -> Path` (asks for public URL, LLM upstream address+key, CI address+token, Postgres/MinIO credentials generated with `secrets`, web bind/port, host list with roles; writes 0600, validates; refuses to overwrite); `def render_compose(cfg: FleetConfig, out: Path) -> None` (0600 env file; values escaped for compose `env_file`).

- [ ] **Step 1: Write failing tests**: `test_init_writes_0600_and_validates`, `test_init_refuses_overwrite`, `test_mint_token_hash_roundtrip`, `test_render_compose_preserves_dollar_quote_newline_values`, `test_join_adds_host_and_never_prints_token_in_report`.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_fleet_config_cli.py -q`; expect FAIL.
- [ ] **Step 3: Implement**; the join report contains `token: "<written to host>"`, never the value.
- [ ] **Step 4: Run** the same; expect PASS.
- [ ] **Step 5: Commit** `feat(fleet): config init/validate/token/render-compose and join credentials`.

### Task 4: Python host client and consumer migration

**Files:**
- Create: `herdr_engineering/host_config.py`
- Modify: `herdr_engineering/service_metadata_feed.py`, `mission_activity_feed.py`, `session_env.py`, `browser.py`, `plannotator.py`, `cli.py` (every `os.environ.get` for `HERDR_CP_*` / `HERDR_ENGINEERING_*` configuration)
- Test: `tests/test_host_config.py` plus updated consumer tests

**Interfaces:**
- Produces: `def load_host_config(*, home: Path | None = None, fetch=urllib-based default) -> HostConfig`; `HostConfig.get(key: str, default=None)`, `.control_plane_url`, `.api_key`, `.revision`. Writes cache `~/.cache/herdr-engineering/fleet-config.json` (0600). Raises `HostConfigUnavailable` when neither fetch nor cache works. Posts `applied` after a successful fetch (best effort).

- [ ] **Step 1: Write failing tests**: `test_fetch_success_writes_cache_and_reports_applied`, `test_unreachable_uses_cache`, `test_unreachable_no_cache_raises`, `test_401_discards_cache` (Review Focus: revoked token).
- [ ] **Step 2: Run** `python3 -m pytest tests/test_host_config.py -q`; expect FAIL.
- [ ] **Step 3: Implement**, then replace each env read listed above with `HostConfig`; delete the env fallbacks and their `--control-plane` env defaults.
- [ ] **Step 4: Run** `python3 -m pytest tests -q`; expect PASS (known flake `test_persist_merges_concurrent_external_lease` may need a rerun).
- [ ] **Step 5: Commit** `refactor: read host config from the central file, drop config env vars`.

### Task 5: Plugin (JS) client

**Files:**
- Create: `deploy/herdr-cp-feed-plugin/host-config.js`
- Modify: `emit.js` (`resolveUrl`), `snapshot.js` (API key, aliases), `herdr-cp-sessions.service` and launchd plist generation in `link-remote.sh` (remove `HERDR_CP_URL` and key `Environment=` lines)
- Test: `deploy/herdr-cp-feed-plugin/host-config.test.js`, updated `emit.test.js`, `snapshot.test.js`

**Interfaces:**
- Produces: `export async function loadHostConfig({home, fetch}): Promise<{url, apiKey, revision, values}>` with the same cache/401/unavailable semantics as Task 4; throws `Error("host config unavailable: ...")`.

- [ ] **Step 1: Write failing tests**: `fetches and caches`, `uses cache when fetch rejects`, `401 clears cache and throws`, `resolveUrl reads host.json not env`.
- [ ] **Step 2: Run** `node --test deploy/herdr-cp-feed-plugin/host-config.test.js`; expect FAIL.
- [ ] **Step 3: Implement** and wire into `snapshot.js` startup.
- [ ] **Step 4: Run** `npm test`; expect all pass.
- [ ] **Step 5: Commit** `feat(plugin): central host config`.

### Task 6: Compose and deploy scripts

**Files:**
- Modify: `deploy/control-plane/deploy.sh`, `deploy/control-plane/compose.yaml`, `deploy/deploy-fleet-agents.sh`
- Delete: `deploy/control-plane/generate-env.sh` (its generation moves into `init_config`)
- Test: `tests/test_deploy_scripts.py` (dry-run output only)

- [ ] **Step 1: Write failing test**: `test_deploy_dry_run_renders_env_from_central_file_and_removes_it` (dry-run prints the render step and no path under `~/.config/.../control-plane.env`).
- [ ] **Step 2: Run** `python3 -m pytest tests/test_deploy_scripts.py -q`; expect FAIL.
- [ ] **Step 3: Implement**: `deploy.sh` runs `herdr-eng fleet config render-compose --out "$(mktemp)"`, passes it as `HERDR_CP_ENV_FILE`, removes it on exit (trap); compose mounts the central file read-only at `/etc/herdr/fleet-config.yaml` and the web command gets `--fleet-config`; `deploy-fleet-agents.sh` copies no env, writes `host.json` check only.
- [ ] **Step 4: Run** the test and `deploy/control-plane/deploy.sh --dry-run --ref HEAD --no-fetch`; expect the render step in the output.
- [ ] **Step 5: Commit** `feat(deploy): render compose env from the central config`.

### Task 7: Hosts page drift badge

**Files:**
- Modify: `packages/hosts-view.js` (or the file that renders the Hosts table; locate by `fleet/inventory` fetch), `packages/` client for `/api/v1/fleet/config/status`
- Test: `tests/*.test.js` alongside the existing hosts view tests

- [ ] **Step 1: Write failing test**: host with applied revision ≠ current shows "Config behind"; equal shows "Config current"; no value ever rendered.
- [ ] **Step 2: Run** `npm test`; expect FAIL.
- [ ] **Step 3: Implement** the badge and status fetch.
- [ ] **Step 4: Run** `npm test`; expect PASS.
- [ ] **Step 5: Commit** `feat(web): config revision badge on Hosts`.

### Task 8: pi-engineering (separate repo and PR, base `origin/main`)

**Files:**
- Modify: `src/runtime/herdr/InspectorFeed.ts` (`inspectorConfigFromEnv` → `inspectorConfigFromHost`), callers in `src/runtime/herdr/index.ts`
- Create: `src/runtime/herdr/HostConfig.ts`
- Test: `test/unit/inspector-feed.test.ts`, `test/unit/host-config.test.ts`

**Interfaces:**
- Produces: `export async function loadHostConfig(opts?: {home?: string; fetch?: typeof fetch}): Promise<{url: string; apiKey?: string; values: Record<string, unknown>}>`; feed enabled when `values["inspector.enabled"] === true`; reasoning from `values["inspector.reasoning"]` (`off|summary|full`).

- [ ] **Step 1: Write failing tests**: feed attaches with no env set when central config enables it; stays off when disabled; cache and 401 behave as in Task 4.
- [ ] **Step 2: Run** the repo's unit test command for these files; expect FAIL.
- [ ] **Step 3: Implement** and remove `PI_INSPECTOR_FEED`, `PI_INSPECTOR_REASONING`, `HERDR_CP_URL`, `HERDR_CP_API_KEY` reads.
- [ ] **Step 4: Run** the full unit suite; expect PASS.
- [ ] **Step 5: Commit**, open its own PR.

### Task 9: Cutover on the live fleet

- [ ] **Step 1:** On bender run `herdr-eng fleet config init`, entering the current values (copied from the existing `control-plane.env`, not retyped into chat or a commit). Run `fleet config validate`.
- [ ] **Step 2:** Merge PRs (herdr-engineering first), run `deploy/control-plane/deploy.sh`, confirm `/api/v1/health` commit and `fleet/config/status`.
- [ ] **Step 3:** Join each host (`fleet join` credentials step), run `deploy/deploy-fleet-agents.sh`, confirm each host's applied revision equals current and feeds report `errors:0`.
- [ ] **Step 4:** Remove the old `control-plane.env` and the hand rollout override only after step 3 passes and the user confirms.
- [ ] **Step 5:** Verify a new pi session on beast shows inspector data on its session page.

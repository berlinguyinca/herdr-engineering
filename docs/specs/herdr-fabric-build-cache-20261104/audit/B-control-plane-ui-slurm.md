# Audit B: control plane, dashboard, metrics, Slurm, config, CLI (what EXISTS)

Repo: /home/user/herdr-engineering-wt/fabric-build-cache, branch feat/fabric-build-cache, HEAD 0ab6931c.
All paths below are relative to that root. Read-only audit; nothing was modified. The untracked
docs/specs/herdr-fabric-build-cache-20261104/ was ignored.

## 0. Headline findings (read first)

1. A "build" subsystem ALREADY EXISTS, per host and local only: `herdr_engineering/build_coord.py` (flock slot gate,
   `herdr-eng build-coordinate`), `build_cleanup.py` (Cargo `target/` pruning, `herdr-eng build-cleanup run|watch|status`),
   `build_maintenance.py` (companion that runs while the local herdr server lives), docs/build-gate.md, docs/build-cleanup.md,
   `deploy/pi-build-gate/build-gate.ts`. There is NO control-plane/fabric-level build-cache concept: no `build-cache`
   string in routes, no table, no event type, no dashboard view. A `build` entity_type exists only inside the redaction
   allowlist (api.py:5977, `_redact_event`: `{"test_run","build","review","review_finding","pull_request"}` use `redact_delivery`).
2. Slurm is READ-ONLY capacity probing for assistant placement. There is NO job-submission runner anywhere (section 6).
3. There is NO Prometheus exporter, `/metrics` endpoint, remote_write or Grafana integration in the repo (section 5).
4. SSE has NO `Last-Event-ID` resume and no `id:` field; reconnect = replay the last 100 events + client dedupe (section 2).
5. Control-plane host/fleet state is an in-memory store (event-sourced into Postgres `fabric_events`), not relational
   tables per host (section 3). New durable records should follow the migration + store pattern of artifact shares (0041).
6. Slurm binaries are not installed on this host (`command -v sbatch squeue sinfo sacct scancel srun`: all absent).

## 1. Control-plane API (herdr_engineering/control_plane/api.py, 6249 lines)

Server model: stdlib `ThreadingHTTPServer` + `BaseHTTPRequestHandler` built by `_make_handler(server, public, operator)`
(api.py:1952). Not a framework router. Three listener flavours: default, `public=True`, `operator=True` (a collab/HCN
flavour; see api.py:2930-2960 `_collab...`).

### Route registration and naming
- `_route_api` (api.py:3042) splits `self.path` into `parts` (`["", "api", "v1", <resource>, ...]`, `resource = parts[3]`),
  applies `_rate_limit()`, then tries delegated route modules IN THIS ORDER, each returning True when it answered:
  `route_collab` (artifact_collab_api.py), `route_report` (artifact_report_api.py), `route_share` (artifact_share_api.py),
  `route_registry` (artifact_registry_api.py), `route_skills` (skill_routes.py). Only then falls to inline
  `_api_get(parts, resource)` (api.py:3362) or `_api_post(parts, resource)`.
- A NEW `/api/v1/build-cache/*` family should be a separate module with `route_build_cache(handler, server, parts, resource) -> bool`
  inserted next to those five imports (api.py:3050-3062), NOT more `if resource == ...` blocks inside the 6k-line file.
- Resource names are kebab-case plural nouns (`assistant-proposals`, `review-gate`, `mission-outcomes`, `ai-sync`, `fabric-services`).
  Sub-actions are trailing path segments (`.../reservations/commit`, `.../shares/{sid}/approve`).
- Exceptions raised inside route code are mapped centrally in `_route_api` (api.py:3064-3110): KeyError->404,
  ValueError->400 (`code=="not_found"`->404), PermissionError->403, EnvelopeConflict/SequenceCollision->409,
  ArtifactConflict->409, RuntimeError->503 "Integration service is unavailable." (message never echoes internals).
- Request metrics route labelling (`request_metrics.py:RequestMetrics.route`, lines 13-30) whitelists resource names; any
  resource not in that set is bucketed as `other`. A new `build-cache` resource must be added to that set to be visible
  in `/api/v1/health` performance (api.py:1624 `h["performance"]`).
- Capability advertisement: `ControlPlaneServer._capabilities()` (api.py:1570-1595) returns strings like `skill_registry_v1`,
  `deployments_v1`, `architecture_v1`, appended only when the feature is enabled. The dashboard nav hides entries until
  the capability is present (section 4). Add `build_cache_v1` here, gated by the flag.

### Pagination convention
- Plain lists: `?limit=` parsed with `_qint(self.path, "limit", default)` (api.py:6166) and clamped inline, e.g.
  `min(max(_qint(...,"limit",100),1),500)` (api.py:3572, hosts telemetry). No offset.
- Cursor pagination exists in newer modules: `artifact_context.py:146-149` (`next_cursor` from `(created_at, id)`),
  `durable_history.py:127` (`next_cursor`, `more`), `artifact_registry_api.py:74` (`cursor=query.get("cursor")`),
  `federation_export.py:132-135` (cursor + limit validation). Response shape: `{"items"/named list, "next_cursor": str|None}`.
  Copy durable_history/artifact_registry_api for build-cache record listing.

### Auth / authorization model
- Roles come from API keys: `server._keys` maps key -> role (`_role`, api.py:1960-1983). `secrets.compare_digest` on `X-API-Key`.
  No keys configured = "open" mode returns role `operator` (loopback / explicit allow-open only).
- `_require(min_role)` (api.py:2080-2130): `viewer` = read-only (also what unknown roles are treated as); `operator` = read +
  mutate; `ingest` = read + `POST /api/v1/ingest` only; `agent` = ingest + host-side tool routes (`_AGENT_POST`,
  `_REPORT_AGENT_POST` regexes); `reviewer` = read-only + four review-gate worker POSTs. MUST be used as
  `if self._require(...): return` (returns truthy on refusal).
- GET and POST are gated generically before routing: GET at api.py:2406 (`_require("viewer")`), POST at api.py:2483
  (`_require(min_role)` where min_role is operator unless the path is in an allowlist). So a new POST route is operator-only by
  default with no extra code; a new agent/host-token POST route needs an explicit regex entry like `_AGENT_POST`.
- Principals: `_principal()` (api.py:2000-2016) returns `{"kind","id": "key:<sha256[:12]>","authenticated_by"}`; the key is never
  recorded. `_resource_principal()` (api.py:2043) = `api-key:<sha256[:16]>:<role>`, used for per-resource owners.
- Host-side writers use a FLEET HOST TOKEN (Bearer / `X-Host-Token`) hashed in the fleet config (`deployment_actions.py` header,
  `fleet_config.py`), distinct from API keys.
- Feature-specific viewer mapping: `project_intelligence_api.py` maps `key:<digest>` to `{org_id, projects, clearance}` in YAML.
  Non-visible and non-existent resources both 404 (non-disclosing). Copy that rule.
- Cross-origin mutation guard for browser POSTs: `artifact_share_api.py:362-366` (Origin netloc must equal Host).

### Idempotency
- `intelligence` POST maps the `Idempotency-Key` header into `payload["idempotency_key"]` (api.py:3915-3918). Scheduler
  reservations / connector calls take `idempotency_key` in the body (api.py:4867, 4942, 5016-5030, 5094).
- Strongest semantic reference: `collab_intents.py` (header comment lines 13-50): decided ticket of same actor+kind+key is replayed
  when `request_sha256` matches, else rejected with REASON_KEY_REUSED (line 210) "idempotency key was reused for a different request".
  `artifacts.py:519/538` (`merge_commit_for_key`, `_merge_replay`) does the same keyed on `(created_by, key)` + request sha.
- Rule to copy: key + canonical request hash; same key + same hash = replay original result; same key + different hash = 409/reject.

### Error and redaction conventions
- Errors: `{"error": "<short message>"}` (+ optional `"reason"`/`"code"`), via `_json(data, status)` (api.py:5674) / `_send_json` (5952) /
  `_deny` (2017; closes connection because the body is unread). Never echo internals (api.py:2972 "internal error" 500).
- Disabled feature answers 404 `{"error": "not found"}` (skill_routes, intelligence) or 503 `... is not enabled` (shares). Pick 404 for
  default-off flags so the nav hides the page.
- Redaction: `redact.py` (`redact_payload`, `redact_delivery`, `_PATTERNS`, `_SECRET_KEY_HINTS`) is applied to ALL SSE/event payloads in
  `_redact_event` (api.py:5971). Keys containing password/token/secret/dsn/credential/auth are blanked wholesale. Build logs and
  command lines must be passed through it before storage or response.
- Body limits: `_read_json(limit=_MAX_JSON_BODY)` (api.py:5657); 413 via `_content_length` for the collab path.
- ETag: `etag_json` / `_etag_json` (api.py:6174-6190, tests/test_etag.py) for cheap polling reads of lists.

### Two closest analogue modules (copy these)
1. **`artifact_share_api.py` (399 lines) + `artifact_shares.py` + migration `0041_artifact_shares.sql`**: records + stats + operator action
   with a preview/confirm gate. Pattern: module docstring lists every route and its permission; `route_share(handler, server, parts,
   resource) -> bool` returns False for non-matching paths (no side effects), validates method per sub-action first, checks feature
   wiring (503), Origin check on POST, permission (404 non-disclosing), then dispatches. `POST .../shares` creates a `pending` row
   and DRY-RUNS the bundle (names, sizes, warnings) with no side effects; `POST .../shares/{sid}/approve {"confirm": true}`
   performs the work (refuses unless confirm is exactly True, lines 389-390); `GET .../shares/{sid}` re-shows the same dry-run
   summary while pending; token returned ONCE, only sha256 stored. State machine pending -> live -> revoked, append-only
   `artifact_share_events` audit table. This is the template for a build-cache "plan/preview -> confirm token -> execute".
   Note: it is a *state-row* preview, not a signed preview token; a stateless preview token would be new work (HMAC over
   request hash + expiry; see `deployment_actions.py` `force-<token_hex>` ids at line 227 and `assistant.py:preview_proposal` line 3792).
2. **`scheduler_service.py` + inline routes (api.py:3408 GET, 4064+ POST) + `fabric_scheduler` migrations 0011/0005**: `plan`
   (read-only) -> `reserve` (201) -> `commit` / `claim` / `retry` / `release` / `cancel`, leases with generation+token heartbeat, and
   `scheduler_admission` default-OFF flag (ctor param `scheduler_admission=False`, api.py:307; disabled returns 503
   "scheduler admission is disabled", api.py:4071). Use for lifecycle semantics of long-running build jobs.
   Also useful: `project_intelligence_api.py` (`get(parts, query, principal, role)` / `post(...)` return `(status, body)`, status route
   with operator-only extra counters, lines 317-372) for stats endpoints.
   `skill_routes.py` is the cleanest template for the flag-gated construction (`build_skill_registry` returns `(None, None)` when off).

## 2. SSE

- Endpoint: `GET /api/v1/events/stream` (api.py:3601 -> `self._sse()` at api.py:5519). Per-entity polling form:
  `GET /api/v1/events?entity_type=&entity_id=` (api.py:3603+; blocks `lineage`, `review_request`, `collab*` entity types).
  Other SSE endpoints: inspector stream `sessions/{id}/inspector/stream` (api.py:5406, `_inspector_sse` 5427), collab `_collab_sse` (2970),
  `rakazo_bridge.py:1707`, `rakazo_agents.py:202`.
- Envelope (global stream): each frame is `data: <json>\n\n` only (`_sse_frame`, api.py:5991; no `id:`, no `event:` field) with the
  Event dict: `event_id` (`event_<hex>`), `schema_version`, `source_timestamp`, `ingest_timestamp`, `entity_type`, `entity_id`,
  `mission_id`, `session_id`, `sequence` (per `(entity_type, entity_id)` stream), `event_type`, `payload` (redacted).
  Envelope v2 optional fields in `events.py` Event (source, actor, visibility_scope, correlation_id, trace_id, ...); contract doc
  docs/contracts/event-envelope-v2.md.
- Bookkeeping frames: `StreamConnected` on open, `StreamKeepalive` every `_SSE_IDLE_SECONDS` on quiet; the client filters both
  (packages/herdr-web-client/events.js:35).
- Resume: NONE via `Last-Event-ID` (grep finds no server reference). On connect the server replays `repo.recent_events(limit=100)` (api.py:5538)
  then streams live. Streams are lifetime-bounded by `_MAX_SSE_SECONDS`; client auto-reconnects after 3 s and dedupes with a
  500-entry `seenIds` set on `event_id` (events.js:11-26). Slow subscribers (queue maxsize 256) are dropped and must reconnect (api.py:1633-1641).
- Auth: same gate as any GET (`_require("viewer")` before route). Stream opens are rate-limited separately via `server._stream_limiter`
  (api.py:3017). Inspector stream additionally has a per-session `can_subscribe` cap (429 "too many streams").
- Publishing: `Repo.record_event(entity_type, entity_id, event_type, payload, mission_id, session_id, require_durable, publish, envelope, feed)`
  (repo.py:2791) assigns the per-stream sequence, writes to the sink (Postgres `fabric_events`), and calls registered listeners;
  `ControlPlaneServer` registers `repo.subscribe(self._broadcast)` (api.py:772; `Repo.subscribe` repo.py:353). So a new
  `BuildCacheEntryRecorded`-style event is `repo.record_event("build_cache", <id>, "<EventType>", {...})`; it reaches the dashboard
  automatically with no SSE code. `feed=False` keeps it out of the global feed. Keep payloads small and pre-redacted.
- Dashboard refresh model: views do not consume event payloads; `refresh-bus.js` `liveRefresher` re-fetches on events (optionally filtered by
  an interest set). Host views take every event.
- Event retention: `fabric_events` has retention/archival (migrations 0003_fabric_events_retention_index, 0035_fabric_events_archive;
  `fabric_compaction.py`, off unless enabled, `dry_run` default true).

## 3. Persistence and host/fabric inventory

- Postgres is the durable store (asyncpg; pyproject deps asyncpg, psycopg2-binary). DSN from fleet file `control_plane.pg_dsn`
  (db.py:control_plane_config; defaults to postgresql://herdr:herdr@127.0.0.1:5432/herdr as a dev default only). No SQLite.
- Migrations: `deploy/control-plane/migrations/*.sql`, 42 files, `0001_control_plane.sql` .. `0045_artifact_collab_bridge.sql`. Next free number
  is **0046**. Notes: numbering has gaps/duplicates (no 0039; two `0043_*`; three `0003_*`), applied in sorted filename order by
  `db.py:Migrator.apply` (db.py:65) which records versions in `schema_migrations`, with `MIGRATION_ALIASES` (db.py:16) for the renamed
  early files. Style (0041): header comment citing the spec, "Additive and idempotent. Safe to re-run.", a `-- DOWN:` comment with the
  DROP, `CREATE TABLE IF NOT EXISTS`, CHECK constraints for enums/state, text PKs, `timestamptz NOT NULL DEFAULT now()`, append-only
  `*_events` audit table with an `ingest_key` for idempotent ingestion.
- Store pattern for a new feature: a class with injected `persist=` callable (see `Repo.__init__` repo.py:243-282:
  `SourceLedger`, `AgentIdentityRegistry`, `InspectorStore`, `EscalationLedger`, `HostMetricsStore`, `DeploymentsMonitor` all take a
  persist hook that appends events through the sink and rehydrate from events). Many control-plane stores are IN-MEMORY, rebuilt from
  `fabric_events`; relational tables are used for artifacts/shares/collab/work graph/team tasks/assistant (async stores with a real conn).
  Tests for DB-backed stores use real Postgres (`test_scheduler_postgres.py`) per the repo's no-mocks rule.
- Host registry: `Repo._hosts` dict; `register_host` (repo.py:1050), `upsert_host(host_id, host_name, tailnet_ip, cpu, mem, status,
  inventory, host_uid, aliases, platform, scheduler_owned/trusted/policy/capacity)` (repo.py:1298), `get_host` / `list_hosts`
  (repo.py:1063/1115; `_merged_hosts` folds aliases and picks the freshest `last_seen`; `_with_problems` attaches `problems`,
  `problem_count`), `record_telemetry` / `list_telemetry` / `host_history(host_id, range)` (repo.py:1240-1283), `host_snapshot` (scheduler view).
  Events: `HostRegistered`, host heartbeat events (tests/control_plane/test_host_heartbeat_events.py).
- Routes: `GET /api/v1/hosts`, `GET /api/v1/hosts/{id}` (ETag), `GET|POST /api/v1/hosts/{id}/telemetry` (api.py:3566-3578, 4188),
  `GET /api/v1/fleet/inventory` (api.py:3553 -> `Repo.fleet_inventory` repo.py:1180: host x component version matrix with `drifted`/`missing`
  per cell, reference = most common version), `GET /api/v1/fleet/config/status`.
- Freshness/heartbeats: hosts post telemetry/heartbeats ~30 s; routine heartbeats are held in memory without an event; a silent host never
  emits an event, so staleness is computed on read (`host_problems.py:compute_problems`, 162 lines, tests/control_plane/test_host_problems.py)
  and in the client (`host-view.js:staleFor`, `hosts-view.js` 15 s tick and `HOST_POLL_MS = 15000` poll). `host_metrics_store.py`
  (330 lines) holds time series; host capacity for placement is `scheduler_capacity` / `host_policy.py` / `host_config.py`.
- The inventory collector shape (`inventory: {tools: {name: version}, plugins: {...}, collected_at}`) is produced by the fabric agent
  (`herdr_engineering/fabric_agent.py`, `fleet.py`); a per-host build-cache report can ride the same upsert/telemetry path rather than a new ingest.

## 4. Dashboard (packages/, plain ES modules, no bundler)

Layout: `packages/*.js` are views and helpers; `packages/herdr-web-client/` (client.js `HerdrClient`, events.js, tests),
`packages/herdr-web-components/` (custom elements: `components.js`, `index.js` `defineComponents()`, `app-shell.js`, `status.js`,
`format.js`, `humanize.js`, `timeline.js`, `density.js`, `components.css`, plus tests), `packages/herdr-design-system/` (base.css, theme.css, README).
`package.json` is `"type": "module"`, only devDependency `playwright`.

- Router/nav: `packages/app.js` (1175 lines). `NAV` array (app.js:85-102) has `{group, path, label, match?, capability?}`; groups Monitor,
  Agents, Fabric, Delivery, Code. `SECTION_TABS` (app.js:107-116) defines tab strips per section: Fabric hosts section is
  `[['/hosts','Hosts'],['/scheduling','Scheduling'],['/ai-sync','AI sync']]` (app.js:108). Route table maps path -> view function
  (app.js:225-260, e.g. `'/hosts': viewHosts`, `'/hosts/:id': viewHost`, `'/ai-sync': viewAiSync`, `'/scheduling': viewScheduling`).
  Capability gating: `nav-capabilities.js` `visibleNav(nav, capabilities)` hides entries whose `capability` is not in `/api/v1/health`
  `capabilities`; optional tabs likewise (app.js:179). A page returning 404/501/"not enabled" is auto-marked unavailable via the `MISSING`
  regex (app.js:121-130) and hidden (localStorage `herdr-unavailable-pages`), so a default-off backend route needs no extra hide code.
  Registering a view = (1) add path to `NAV.match` or a new NAV entry / `SECTION_TABS` tab, (2) add to the route table, (3) add the
  `viewX` function mounting a `mountX(container, client, ...)`; also keep `KNOWN_PAGES` (derived) in sync automatically.
- **Best host for a "Build cache" panel**: the Fabric > Hosts section.
  - Fleet-wide: add a tab `['/build-cache','Build cache']` to the hosts SECTION_TABS entry (app.js:108) and `/build-cache` to the Hosts NAV `match`
    list (app.js:92), with a new `build-cache-view.js` modelled on `hosts-view.js` / `ai-sync-view.js` (+ `.test.js`), gated by capability `build_cache_v1`.
  - Per-host: `host-detail-view.js` (74 lines; `mountHostDetail`, sections keyed `data-section`, morphed) and `host-view.js` (286 lines;
    `renderHostCard`, `renderHostDetail`, `HOST_CARD_STYLE`, `RANGES`, `staleFor`) are where a per-host build-cache section/card belongs.
  - Reuse: `fleet-view.js` `renderFleetMatrix` (host x component drift matrix, 45 lines) for a host x cache-state matrix; `patch.js`
    (`patchList`, `morph`, `patchText`), `motion.js` (`snapshotText`, `animateChanges`), `refresh-bus.js` (`liveRefresher`,
    `pollWhileVisible`), `freshness.js` + `<herdr-freshness>`, `record-state.js`, `dashboard-load-state.js`, `fleet-config-badge.js`, `related-panel.js`,
    `<herdr-empty>`, `<herdr-error>`. Client helper: `client.request(path)` generic, or add typed methods to `HerdrClient` (client.js) with a test
    in packages/herdr-web-client/.
  - Other views: `scheduler-view.js` (+test) is the nearest "records + operator action" UI; `deployments-view.js`, `ci-health-view.js`, `ai-sync-view.js`.
- JS tests (exact commands, from package.json): `npm test` = `node --test --test-concurrency=1 packages/herdr-web-client/*.test.js
  packages/herdr-web-components/*.test.js packages/*.test.js 'tests/playwright/!(ui-gates-)*.test.mjs'`;
  client-only quick loop `npm run test:client` = `node --test packages/herdr-web-client/*.test.js packages/herdr-web-components/*.test.js packages/*.test.js`;
  single file `node --test packages/<name>.test.js`; browser tests `npm run test:ui`; gates `npm run test:ui-gates`
  (`node --test --test-concurrency=1 tests/playwright/ui-gates-*.test.mjs tests/visual/*.test.mjs`).
- Browser/E2E: Playwright via node:test (`tests/playwright/*.test.mjs`) against small Python fixture servers (`tests/playwright/server.py`,
  `real_server.py`, `*-server.py` such as `deployments-server.py`, `ci-health-server.py`, `artifact-shares-server.py`). Hosts-specific:
  `live-hosts.test.mjs`, `motion-hosts.test.mjs`, `nav-availability.test.mjs`, `nav-groups.test.mjs`.
- UI gates: thresholds in `tests/playwright/ui-gates.yaml` (viewports desktop 1280x800, tablet 768x1024, mobile 375x667, reflow 320x568;
  screenshot `max_diff_pct: 0.01`, `component_max_diff_pct: 0.05`, perf budgets, design-token ratchets `max_raw_hex: 5`, `max_off_grid_spacing: 76`).
  Gate files: `ui-gates-{a11y(dashboard-a11y), keyboard, interaction, perf, load, states, tokens, xss, render-xss, taint, mock, real, floor, workbench, checker, visual}`.
  Screenshots: `node tests/playwright/visual-baselines.mjs check [slug]` compares against `docs/assets/screenshots/<slug>__<viewport>[-dark].png`
  (light AND dark required, fonts bundled in tests/playwright/fonts); `... update --i-understand-this-rewrites-baselines [slug]` only on a clean tree.
  New dashboard surfaces are registered in `tests/playwright/visual-baselines.mjs` `SURFACES` and `visual-dashboard.mjs` `DASHBOARD_SURFACES`.
  CI: `.woodpecker/ui-gates.yml`, `.woodpecker/unit-gates.yml`, `.woodpecker/artifact-handoff.yml` (GitHub Actions is disabled org-wide).
  Docs: docs/operations/ui-gates.md. Token and XSS gates mean new views must use design tokens and escape all server strings.

## 5. Metrics

- No Prometheus anywhere: grep for prometheus/openmetrics/grafana/remote_write/node_exporter/otel finds only incidental mentions: fabric_agent.py:67
  (port 9090 listed in a well-known-daemon exclusion), docs/architecture/dev-fabric-gateway.md:106, docs/operations/collaboration-network.md:101
  ("Prometheus export yet (`CollabService.stats` counters only)"), docs/superpowers/specs/2026-10-23-host-telemetry-and-fleet-provisioning-design.md:45
  ("No replacement for Prometheus/Grafana"). There is no `/metrics` route and no text-exposition code. `grafana.metabolomics.us` is not
  referenced in code.
- What does exist (candidates to feed an exporter, do not duplicate):
  - `request_metrics.py` `RequestMetrics`: per-route count/errors/timeouts/status histogram/p95 latency, route labels bounded by whitelist
    (cardinality guard: arbitrary path segments never kept, `:id` placeholders, depth capped at 4). Served inside `/api/v1/health` -> `performance`.
  - `observability.py` `MetricsRegistry` + `METRIC_NAMES` (spec 0170: machine_reconnects, event_ingest_lag_sec, dev_lease_*, web_latency_ms, ...): in-memory
    counters, snake_case names, no labels.
  - `host_metrics.py` (715 lines, host sampler), `host_metrics_store.py` (series store), `llm_stats.py`, `usage_spend.py`, `lineage_metrics.py`,
    `project_intelligence_api.py` `metrics_fn` ("counters only: no content, ids or titles"), `CollabService.stats`.
- Conventions to inherit: counters only, no content/ids/titles, bounded label sets. For Grafana, the Prometheus data lives on the lab side
  (`grafana.metabolomics.us` not in this repo); an exporter here would be new code behind its own flag and auth (the existing `/api/v1/*` roles).
  MEMORY notes (not repo): control-plane metrics for other systems use remote_write; confirm in the other audit tracks.

## 6. Slurm (precise)

Verdict: READ-ONLY capacity probe + placement scoring. NO job-submission runner exists.

- `herdr_engineering/control_plane/slurm_placement.py` (171 lines). Module docstring: "Read-only Slurm capacity for assistant placement ...
  Nothing here submits a job. Launching onto a cluster needs a runner installed and attested there, which is a separate change."
  - `slurm_config(value)` validates `personal_assistant.placement.slurm.clusters` (max 16 clusters, 32 partitions each; keys
    id, label, login_host, assistant_use, partitions{capabilities, profiles, role_bindings}); clusters without `assistant_use: true` are ignored.
  - `SINFO_FORMAT = "%P|%a|%n|%T|%C|%e|%m|%G"`, `parse_sinfo` (usable states only idle/mixed; GPUs counted only on nodes with zero allocated CPU),
    `ssh_argv(login_host)` -> `ssh -o BatchMode=yes -o ConnectTimeout=5 <host> "sinfo -N -h -o '<fmt>'"` (host validated by `_HOST_RE`, leading `-` refused),
    `_ssh_runner` (timeout 12 s, fixed argv, no user input), `SlurmProbe(clusters, runner=, ttl=60, clock=)` with `.candidates()` -> rows
    `{id:"slurm:<cluster>:<partition>", kind:"slurm", backend:"slurm", status:"ready"|"unavailable", capabilities, nodes, free, profiles, role_bindings, ...}`.
    An unreachable cluster is "unavailable", never an exception. The only external command is `sinfo` over ssh.
  - Wired in `assistant.py:47,372-373,2788-2789,4303` (`self.slurm_probe`, merged into `resolve_placement` candidates with `host_candidates`) and served by
    `GET /api/v1/assistant-placement`, `assistant-route`, `assistant-graph`.
- `tests/control_plane/test_placement.py` (334 lines): pure placement tests plus a slurm block (lines 80-210): config validation/ignoring, `sinfo`
  parsing (including real whiteale fixture `tests/fixtures/slurm/whiteale-sinfo-nodes.txt`), free-memory cap, per-node fit (one node must cover all resources),
  probe caching + failure=unavailable, ssh quoting test, role/profile binding. `test_live_cluster_probe_returns_well_formed_candidates` is skipped unless
  env `HERDR_LIVE_SLURM_HOST` is set and states "Read-only: one `sinfo` over ssh ... Never submits or installs anything."
  Tests inject `runner=lambda host: SINFO` (a function seam, not a mock framework).
- Cluster targeted: none hard-coded. Cluster ids/login hosts are operator YAML (`personal_assistant.placement.slurm.clusters`). Fixture is from whiteale
  (a lab host running Slurm), tests use ids like `hive`. Nothing in code names Farm/Franklin/Hive for submission.
- Repo-wide grep for sbatch/srun/scancel/squeue/sacct/slurm (code, not docs):
  - `control_plane/assistant_hub/canonical.py:374-1007`: a COMMAND CLASSIFIER for agent-issued shell commands (squeue/sacct/sinfo/sprio/sstat/sshare/sdiag =
    `_CLUSTER_RO`; scancel/salloc/sbatch/srun/sacctmgr = `CLUSTER_MUTATING`, with `_sbatch`/`_srun` parsers at 711-832). It *classifies and gates*, it does not run.
  - `agent_mine.py:62`: a command-name list for transcript mining.
  - `control_plane/deployment_definitions.py:57`: `GPU_RELEASING = {"job-cancel-resubmit", "scancel-resubmit"}`, labels for repair action kinds only.
  - `architecture/runner.py` `AnalysisCancelled` (false positive on "scancel" substring match), `herdr_adapter.py` `_SubprocessRunner` (false positive "srun").
  - `packages/*` mentions are docs/comments ("never scrapes ... Slurm").
- Therefore an optional "Slurm full-build execution" is entirely NEW: needs a submission runner (sbatch via ssh or a login-node agent), account/partition
  config, job id tracking, cancel path, and a gate through the assistant_hub canonical classifier policy. Reuse: `slurm_config` validation style, `_HOST_RE` /
  `_PARTITION_RE` / `shlex.quote` hardening, `SlurmProbe` for capacity, cluster YAML schema. Memory notes (not repo): hive/farm rules (ucd-hpc skill,
  hive-admins-not-an-option, login-node task cap 512) apply to any such runner.
- Slurm on THIS host: `command -v sbatch squeue sinfo sacct scancel srun` -> none found. No local Slurm probing is possible here.

## 7. Config and feature flags

Two config layers, both YAML-only (operator rule: no environment-variable configuration; existing `${VAR}` expansion in config.py is legacy):

A. Per-host/engineering config: `herdr_engineering/config.py` (286 lines).
  - `DEFAULTS` dict (config.py:28-110) is the shipped schema; default-off sections look like `build_cleanup: {enabled: False, roots: [], interval_seconds: 900,
    high_water_percent: 85, low_water_percent: 80, min_age_hours: 24}` and `github.projects: {enabled: False, item_limit: 2000}`.
  - `load_config(paths)` (config.py:~200): discovery order machine-local `~/.config/herdr-engineering/config.yaml` < `./config/herdr-engineering.yaml` or
    `./herdr-engineering.yaml` < `$HERDR_ENGINEERING_CONFIG`; deep-merge onto `DEFAULTS` (`_deep_merge`, never wholesale overwrite), so a legacy config lacking
    a new section loads fine and picks up defaults; only an explicit `version != 1` is rejected. Validators are small pure functions that raise
    `errors.InvalidError` with the key name (`github_sync`, `github_projects`, `validate_github`, called after the merge).
  - To add a section: add the key to `DEFAULTS` with `enabled: False`, add a `validate_build_cache(section)` raising `InvalidError("build_cache.x must be ...")`,
    call it beside `validate_github(cfg.get("github"))`, document it in `config/herdr-engineering.example.yaml` (build_cleanup block at lines 70-78 is the pattern),
    test in `tests/test_config.py`.

B. Control-plane (server) features: central fleet file (`herdr_engineering/fleet_config.py`, schema v1, mode 0600, outside git, `sections` tagged by role + `hosts`
   with token sha256 only). The `control_plane` section is read ONLY by the control-plane process, never served to hosts (`CONTROL_PLANE_SECTION`).
  - Read live via `ControlPlaneServer._control_plane_value(fleet_config, key)` (api.py:792) / `fleet_config.current().section("control_plane")`; the secure
    variant `secure_section` (api.py:891, 956) is used for security-sensitive booleans (e.g. `deployment_actions`).
  - Default-off feature section pattern: `skill_routes.py:skill_registry_config(cfg)` validates a mapping, rejects unknown keys, requires `enabled` bool, requires
    secrets-by-path when enabled, raises `ValueError` (config fails closed at start); `build_skill_registry` returns `(None, None)` when off; ctor param
    `skill_registry=None` (api.py:307); `cli.py:4053` passes `skill_registry=cp.get("skill_registry")`; capability advertised only if constructed (api.py:1584).
    Other flags: `scheduler_admission=False`, `deployment_actions`, `project_intelligence.enabled`, `architecture`, `assistant.enabled`, `ai_sync` (`ai_sync_config.parse_ai_sync`).
  - Example file: `deploy/control-plane/fleet-config.example.yaml` (documents each `control_plane.*` block as commented YAML with `REDACTED` placeholders).
- Secrets rule (must never be embedded): reference by FILE PATH only, mode-checked. Examples: `skill_routes.py` `signing_key_file` (0600, rejects group/other
  bits, 64 hex), `project_intelligence` `webhook_secret_file` (read per delivery so rotation needs no restart), `assistant audit hmac_key_file` ("PATH only"),
  `github.app.private_key_file`, `rakazo` `hmac`/`signing_secret` generated locally "never commit or print it". Example files use `"REDACTED"`; host tokens
  stored as `token_sha256`. A hive/Slurm SSH credential for build execution must follow this (key file path, or rely on the operator's ssh agent/BatchMode).
- fabric (per-host) token location: config.py DEFAULTS comment, `fabric.token` in central host config or `~/.config/herdr-engineering/fabric-token`.

## 8. CLI (herdr-eng)

- Entry: `pyproject.toml [project.scripts] herdr-eng = "herdr_engineering.cli:main"` -> `cli.py:main` (line 4957), parser built by `_build_parser()` (cli.py:36),
  a single argparse tree: `sub = parser.add_subparsers(dest="command", required=True)` then `sub.add_parser("<name>", help=...)`, nested
  `*sub = p.add_subparsers(dest="...")`, handlers bound with `p.set_defaults(func=_cmd_x)` (E702 is deliberately allowed for compact wiring).
  cli.py is 4989 lines; some groups live in their own module with a `register(sub)` function (`assistant_cli.register`, `open_client.register_parser`,
  `fleet_config_cli`, `assistant_manage_cli`, `corrections_cli`, `google_setup_cli`).
- EXISTING build commands (extend, do not duplicate): `build-cleanup {run,watch,status} [--root] [--dry-run]` (cli.py:47-54, `_cmd_build_cleanup` 2769) and
  `build-coordinate [--slots --max-load --wait --bypass-on-timeout --status --json] -- <cmd>` (cli.py:59-81, `_cmd_build_coordinate` 2736).
- A `herdr-eng build-cache ...` group should be a NEW module `herdr_engineering/build_cache_cli.py` exposing `register(sub)` (like assistant_cli) called next to
  `register_assistant_cli(sub)` (cli.py:42), with subcommands such as `status|plan|apply|events` that call `/api/v1/build-cache/*` through the same control-plane
  HTTP helper other control-plane-backed commands use (see how `fleet status`, `scheduler`, `place` commands resolve URL + key in cli.py), and read
  local state via `build_coord` / `build_cleanup` imports (they already expose `--status` and `--dry-run` JSON). Keep `--json` flag convention
  (`_print_json` cli.py:33 uses `indent=2, sort_keys=True`). Add tests in tests/test_cli.py style (real parser invocation).
- Deployed to hosts by Ansible (`ansible/`; `ansible/tasks/pi-build-gate.yml`), docs/build-gate.md says the gate is "implemented, deployed, live-verified".

## 9. Test and lint commands (for the AUDIT's verification section)

- Python: `pytest` (addopts `--import-mode=importlib -m 'not e2e and not integration'`; `integration`/`e2e` need `-m`); `ruff check` (line-length 100,
  select E,F,W,I,UP; E501/E702 ignored); Python >= 3.12. `scripts/validate.sh` is the aggregate gate. Control-plane API tests use a `world`/`hz` harness
  (`h.call(user, method, path)`) in tests/control_plane (see test_placement.py:236-251).
- JS: see section 4. Architecture/isolation tests: tests/architecture/test_isolation.py (greps for forbidden patterns; it mentions sbatch/srun, check before adding any).

## 10. Reuse / do-not-reimplement checklist

- Host registry, freshness, heartbeats, inventory drift matrix, per-host telemetry: reuse `Repo.upsert_host`, `fleet_inventory`, `host_problems`.
- Event publish + SSE + dedupe + dashboard refresh: reuse `Repo.record_event` and `liveRefresher`; do not add a new stream.
- Idempotency: copy `collab_intents`/`artifacts` key+request-sha rule. Redaction: `redact.py`. Pagination: `next_cursor` modules.
- Flag/validation: `skill_registry_config` pattern; capability string `build_cache_v1`; nav `capability:`.
- Preview/confirm: `artifact_share_api` pending-row + `{"confirm": true}`.
- Local build gating/cleanup: `build_coord.py`, `build_cleanup.py`, `build_maintenance.py` already exist per host.
- Slurm capacity read: `SlurmProbe`. Everything about submission, cancel, job tracking, accounting: does not exist.
- Metrics: no exporter exists; counters pattern exists; remote_write/Grafana are net-new.

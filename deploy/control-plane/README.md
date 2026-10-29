# HerdR Dev Fabric Control Plane — Phase 1 Foundation

The control plane's persistence + background worker, deployed as one Docker
Compose project (spec 0190). It is the structured-state backbone the unified
`http://dev.lan` application will consume in later phases.

## Services

| Service              | Image                 | Role                                                    |
|----------------------|-----------------------|---------------------------------------------------------|
| `postgres`           | `postgres:16-alpine`  | Structured state (missions, sessions, services, events, token_usage, state_snapshots, ...). Not SQLite. |
| `rustfs`             | `minio/minio`         | Object store for artifact payloads. **MinIO drop-in** (see below). |
| `control-plane-worker` | `herdr-engineering`  | Bounded background worker: migrations on startup, then stale-lease / health / snapshot / artifact-GC passes. |

## Design notes

- **No SQLite, no Redis/NATS/Kafka.** Structured state lives in Postgres;
  binary/object payloads live in the object store. The control plane reuses the
  existing HerdR fabric/event mechanisms rather than adding a broker.
- **Large binaries never go in Postgres.** Artifact bytes go to the object store
  (content-addressed `sha256/ab/cd/<hex>`); Postgres stores only the artifact
  record + bindings.
- **The worker is separate from the web frontend** (spec §95): GC / retention /
  stale-lease detection / snapshot generation run here, never in the UI.
- **RustFS image (open item).** RustFS does not yet ship a maintained container
  image, so Phase 1 uses MinIO as an S3-compatible drop-in. The client
  (`herdr_engineering.control_plane.rustfs`) is boto3/S3-agnostic, so swapping
  in a real RustFS image later is a config-only change.
- **Bridge networking (not host).** Unlike `deploy/fabric-stack` (which needs
  host networking for the gateway + CoreDNS), this project only needs the
  services to reach each other, so they resolve by service name.

## Configuration and deploy

All configuration, secrets included, lives in the **central fleet config**,
`~/.config/herdr-engineering/fleet/fleet-config.yaml` (mode 0600, never in git), alone
in its 0700 directory (`fleet config init` creates it and leaves a symlink at the old
path `~/.config/herdr-engineering/fleet-config.yaml`). There is no `.env` to write or
maintain.

```bash
# 1) once: create the central file (prompts for the public URL, LLM and CI
#    address/keys and the host list; generates Postgres/MinIO credentials. API keys
#    stay off: control_plane.api_keys is {} and the control plane is open on the tailnet)
herdr-eng fleet config init
#    an EXISTING control plane must keep its Postgres/MinIO credentials (the live
#    volumes were created with them): import the old env file instead - names, never
#    values, are reported; anything still missing is prompted for.
#    See docs/operations/central-config-cutover.md for the full order.
herdr-eng fleet config init --from-env ~/.config/herdr-engineering/control-plane.env
#    non-interactive: --answers answers.yaml (mode 0600; keys public_url,
#    llm_upstream_url, llm_upstream_key, ci_url, ci_token, web_bind, web_port, hosts, ...)

# 2) deploy: validates the file, renders the variables docker compose itself needs
#    (image passwords, ports, bind address) into a transient 0600 file that is removed
#    on exit, builds the image from a git ref, and recreates control-plane-web,
#    control-plane-worker and llm-proxy on that one tag. Nothing on the host changes
#    before the build; slices, the edge nginx.conf and the collab-sync units are
#    installed after it and restored on any failure. Success means the web reports
#    the commit AND every recreated container is running and not restarting.
#    On failure it rolls back to
#    the commit in the running image's receipt with THAT commit's compose files and
#    says honestly whether the rollback came back healthy (exit 1) or not (exit 3);
#    the very first deploy of the central config has no automatic rollback (see
#    docs/operations/central-config-cutover.md)
deploy/control-plane/deploy.sh --dry-run        # validate, render to a temp dir, `docker compose config -q`
deploy/control-plane/deploy.sh                  # --ref, --no-fetch, --fleet-config PATH

# apply migrations / run one pass from the host (needs asyncpg + reachable db; the DSN
# is control_plane.pg_dsn of the central file)
herdr-eng control-plane migrate
herdr-eng control-plane worker --once
```

The web and worker containers mount the central file's DIRECTORY read-only at
`/etc/herdr/fleet/` and are started with `--fleet-config /etc/herdr/fleet/fleet-config.yaml`.
The llm-proxy gets only an llm slice (`fleet config render-llm`: the `llm` section and
`control_plane.llm_proxy`, 0600, in `~/.config/herdr-engineering/llm-proxy/`, mounted
read-only at `/etc/herdr/llm/`) and refuses to start on a file carrying anything else;
the collab container likewise gets only `control_plane.collab`. They read no
configuration from the environment. Do not run `docker compose up` by hand: the
substitution variables only exist while `deploy.sh` runs. Compose refuses to start
without them, and DB/object-store ports stay loopback-only.

Settings of the reserved `control_plane` section the servers read: `pg_dsn`,
`host_aliases`, `api_keys`, `rustfs_*`/`minio_*`, `self_host`, `web_port`,
`ci_actions` (true enables CI retry/cancel), `deployment_definitions` (absolute path, as the
web container sees it, to a checkout of InferWeave's `deploy/pipelines`; read live and validated with
the same rules as its `validate.py`. Absent: no definitions, any host may report any component and the
Deployments snapshot warns. Set: a host may report only components whose `host` is its fleet id, and
an invalid set keeps the last good one, or refuses events with 503 if none was ever valid),
`deployment_actions` (exactly `true` enables the audited Deployments actions; read live, and off while the
file is unusable unless `config_failure_mode: last_good` — see "Deployment actions" below), `inspector_limits` (`max_events`,
`max_bytes`, `max_sessions`, `max_total_bytes`, `retention_hours`, `reasoning_role`,
`sensitive_role`, `store_reasoning`) and `llm_proxy` (`max_inflight`,
`queue_wait_seconds`, `raw_retention_days` (default 14: the web server drops older
`token_usage` records, in memory and in `fabric_events`), `rollup_retention_days`
(default 400, reserved for the hourly rollups)). `control_plane.compose` is the mapping `deploy.sh` renders
for compose itself (e.g. `HERDR_CP_WEB_BIND`, `HERDR_CP_WEB_PUBLIC_PORT`,
`HERDR_CP_PG_PORT`, `GATEWAY_TAILNET_IP`; all eight are required and `fleet config
validate` names any that are missing). `fleet config init` asks for the gateway tailnet
IP, defaulting to `tailscale ip -4`.

The directory (not the file) is bind-mounted because a single-file mount keeps the
old inode: an edit that replaces the file by rename (`sed -i`, vim, our own tools)
would never reach the server. `deploy.sh` refuses to run if the directory holds
anything besides the file, since all of it is exposed to the containers. The tools
that edit it (`fleet join`, `fleet config token`, `fleet config revoke <host>`) take
a dated backup into `~/.config/herdr-engineering/fleet-backups/` (0700, newest 10
kept) and replace the file atomically; the server picks up the new revision on its
next read. Hand edits work the same way. Rewriting the YAML drops comments.

## Web UI + API (`control-plane-web` service)

The `control-plane-web` service serves the control-plane UI, the structured
`/api/v1` JSON API, and the SSE live stream. The web UI is a **client** of this
API (no UI scraping).

```bash
# reachable from the gateway host only (127.0.0.1:8080 by default)
curl http://127.0.0.1:8080/api/v1/health
# open the UI
open http://127.0.0.1:8080/
```

**Auth-free within dev.lan.** The Tailnet is the trust boundary: all machines
belong to each other and are encrypted end-to-end (WireGuard), so the web UI
needs no per-user API keys. The host port is bound to the Tailnet interface via
`control_plane.compose.HERDR_CP_WEB_BIND` in the central file (default `127.0.0.1` = loopback-only), so it is
reachable from any dev.lan machine but **not** from a plain LAN / `0.0.0.0`:

```bash
# expose on dev.lan: answer "web bind address" with this machine's Tailscale IP
# (`tailscale ip -4`) in `herdr-eng fleet config init`, or set
# control_plane.compose.HERDR_CP_WEB_BIND in the central file, then
./deploy.sh
# any dev.lan machine:
curl http://dev.lan:8080/api/v1/health          # 200
```

The web container runs with `--allow-open` (explicit acknowledgment that the
Tailnet replaces API keys). **Do not** change the web bind address to `0.0.0.0`
or drop the Tailnet-IP mapping — that would expose an unauthenticated admin API
beyond the tailnet.

`fleet config init` keeps this posture: it writes `control_plane.api_keys: {}` and
leaves `ingest.api_key` / `agent.api_key` empty, so hosts send no `X-API-Key` at all.

**Optional RBAC (Spec §104) - not supported for the compose stack yet.** Keyed mode
(entries in `control_plane.api_keys`, by hand or with `fleet config init
--with-api-keys`) gates every route except `/api/v1/health` (which then answers only
status, version and commit without a key) and the host endpoints. The browser
dashboard, the llm-proxy and the emit hook do not send keys yet, so turning keys on
breaks them until they do. viewer = read-only, operator = read + mutate, ingest =
POST /api/v1/ingest, agent = ingest + LLM-key mint/revoke + review posting.

Run the same server standalone from the host:

```bash
herdr-eng control-plane web --host 127.0.0.1 --port 8080
herdr-eng control-plane web --api-keys 'k1=viewer,k2=operator'  # RBAC
```

## On-host CLI

The `control-plane` CLI subcommands work against the same config:

```bash
herdr-eng control-plane migrate            # apply versioned SQL migrations
herdr-eng control-plane worker             # run the bounded background worker
herdr-eng control-plane worker --once      # run a single pass and exit
```

On the host without a central file the DSN defaults to the local development database `postgresql://herdr@127.0.0.1:5432/herdr` (password from `PGPASSWORD` or `~/.pgpass`)
(port 5432 is exposed by the compose project). Override via the
`control_plane:` block in the config or `HERDR_ENGINEERING_CONFIG`; with a central
file its `control_plane.pg_dsn` wins.

## Host-side systemd user units

These ship as templates in this directory; nothing installs them. Copy the unit (and its timer, if any) to
`~/.config/systemd/user/`, `systemctl --user daemon-reload`, then enable as shown. Each unit's header lists its
prerequisites. `tests/test_deploy_units.py` checks that every unit parses and that new units read no
`Environment=`/`EnvironmentFile=`.

| Unit | Kind | Enable |
|---|---|---|
| `herdr-ci-fix-runner.service` + `.timer` | oneshot, timer-driven | `systemctl --user enable --now herdr-ci-fix-runner.timer` |
| `herdr-ci-report-relay.service` + `.timer` | oneshot, timer-driven (see `docs/ci-test-reports.md`) | `systemctl --user enable --now herdr-ci-report-relay.timer` |
| `herdr-architecture-serve.service` | long-running worker, no timer; config from `~/.config/herdr-engineering/architecture-serve.yaml` (template `architecture-serve.example.yaml`) | `systemctl --user enable --now herdr-architecture-serve.service` |

The Architecture Explorer worker claims analysis requests for its host only; bind its agent key in
`control_plane.architecture.agent_hosts` and set `require_host_binding: true` in production
(`docs/specs/herdr-codeboarding-native-port-2026-10-23/REPORT.md`, "Flag and rollback").

## LLM keys for sessions

Sessions started with `herdr-eng session run --project P -- <cmd>` mint a per-session key at
`POST /api/v1/llm/keys` and revoke it at `/api/v1/llm/keys/<id>/revoke` when the command ends, so
token analytics are per project without hand-sourcing a file. When this stack runs with
`--api-keys`, the host running sessions needs an **operator** key in
`~/.config/herdr-engineering/control-plane-key` (0600).
A key the host failed to revoke is named on stderr (`key_id`); revoke it with the same endpoint.
See the README section "Sessions get the LLM env automatically".

## Optional agent summary model

Automatic agent summaries can use a separately managed private CPU model. It is
disabled by default and ordinary deploys never download or start it. See
[agent summary model operations](../../docs/operations/agent-summary-model.md) for
explicit provisioning, central configuration, health checks and removal.

## Deployment actions (force redeploy, permit gate, ack)

Hosts pull and herdr never holds a production credential: a force redeploy only **records a
request** that the host updater picks up on its next poll. The routes (each also under `/api/v1/`):

| Route | Credential | What it does |
| --- | --- | --- |
| `POST /api/deployments/force` `{"component": "<name>"}` or `{"stage": <n>}` | operator `X-API-Key` | records a force request for one component, or one entry per component of a stage, with the current plan target. An identical pending request is not duplicated |
| `POST /api/deployments/ack` `{"component", "instance"[, "release"]}` | operator `X-API-Key` | acknowledges a stale instance (for example a preempted node whose `failed` would otherwise halt its stage for that release). The release defaults to the plan target. `{"component", "release"}` with no instance lifts a `failure-evicted` mark. Attempt counts are never changed |
| `GET /api/deployments/requests?host=<id>` | fleet host token | pending force entries for the components defined for that host |
| `DELETE /api/deployments/requests/<id>` | fleet host token | clears the host's own entries of a request, and every other pending entry forcing the same component to the same commit (the "once"). Answers 503 when the clear could not be persisted durably; the host must then NOT redeploy |
| `GET /api/deployments/permit?component=<c>&commit=<sha>[&generation=<n>]` | fleet host token | the host gate: `{permitted, reason, generation, target_commit, forced, request_id}`. With `generation`, a different plan generation answers false ("generation changed") |

- **Deployment actions require API keys.** Force and ack work only in keyed mode, with at least one
  `operator` key in `control_plane.api_keys`. An open tailnet server refuses them with 403, because there every
  caller is the operator, a DNS-rebound page included. The host routes work in both modes.
- **Guards on force and ack:**
  - `control_plane.deployment_actions: true` (otherwise 403).
  - The operator role, by `X-API-Key`.
  - A `Host` a rebinding name cannot reach: an IP literal, `localhost`, or a name listed in the collab dashboard
    hosts.
  - No cross-site `Sec-Fetch-Site`; only `same-origin` or `none` is accepted, and a client that sends none is fine.
  - An `Origin`, when present, whose host equals `Host`. Its scheme may be http or https, because TLS may be
    terminated in front.
  - `application/json`, and a body of at most 4 KiB.
  - Exactly one target, validated against the loaded definitions.
  - A request that carries a fleet host token is refused even beside an operator key.
- **Audit.** Rows are `deployment_action` events, one stream per action per UTC day. A row holds the actor
  (`operator:key:<digest>`), the target, the time and the outcome.
  - An accepted action is audited durably *before* it is recorded. If that row cannot be persisted, the answer is
    503 and nothing is recorded. An ack row also records the acknowledged slot's outcome and `host_ts`.
  - An authenticated operator's refusal is recorded as `refused`. These rows stay out of the shared event feed,
    at most 60 a minute; the rest are counted.
  - Refusals before authentication write NO row: open mode, no or a bad key, a non-operator role, or a host token
    on an operator route. They are counted per reason in memory (the Deployments view, `actions.refused`) and
    logged at most once a minute per reason.
  - Host pickups are recorded as `picked-up`.
- **Requests** are kept until cleared or for 24 h, at most 256 pending (more is refused with 429).
- **Host token scope.** The host token grants only these host routes, and only for the components whose
  definition names that host. Every other route still answers 401.
- **The permit gate is not behind the flag.**
  - A component is permitted when the newest coordinator tick (at most 90 s old, not stale) lists it in
    `permitted` and the commit is the plan target.
  - A pending force for that component and target also permits it, but only where staging holds the plan: a Start
    whose turn has not come, the hold timer, or a halt of the component's OWN stage. A halt in an earlier stage still
    blocks a later stage's force. That tick must also have the release confirmed and the target on main.
  - A force never overrides "release not confirmed", "target no longer on main", an earlier-release or
    unknown-ancestry hold, or a coordinator error.
  - A replan onto another commit voids a pending force.
  - The host sequence is: list requests, ask permit (`forced: true`), `DELETE` the request, redeploy.
- **Turning the flag off** is the kill switch: pending forces are ignored by the gate and the request list is
  empty. The plan's own permission is unaffected.
- **While the fleet file is unusable** (deleted or unparseable after having been valid), the flag reads as off
  (fail closed) unless the last good file set `control_plane.config_failure_mode: last_good`. With `last_good`, a
  flag that was `true` keeps forces live: new force/ack requests are accepted and pending forces still permit.


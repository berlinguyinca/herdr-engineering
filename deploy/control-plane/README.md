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
`~/.config/herdr-engineering/fleet-config.yaml` (mode 0600, never in git). There is
no `.env` to write or maintain.

```bash
# 1) once: create the central file (prompts for the public URL, LLM and CI
#    address/keys and the host list; generates Postgres/MinIO credentials. API keys
#    stay off: control_plane.api_keys is {} and the control plane is open on the tailnet)
herdr-eng fleet config init

# 2) deploy: validates the file, renders the variables docker compose itself needs
#    (image passwords, ports, bind address) into a transient 0600 file that is removed
#    on exit, builds the image from a git ref, and recreates control-plane-web
deploy/control-plane/deploy.sh --dry-run        # validate, render to a temp dir, `docker compose config -q`
deploy/control-plane/deploy.sh                  # --ref, --no-fetch, --fleet-config PATH

# apply migrations / run one pass from the host (needs asyncpg + reachable db; the DSN
# is control_plane.pg_dsn of the central file)
herdr-eng control-plane migrate
herdr-eng control-plane worker --once
```

The web, worker and llm-proxy containers mount the central file read-only at
`/etc/herdr/fleet-config.yaml` and are started with `--fleet-config`; they read no
configuration from the environment. Do not run `docker compose up` by hand: the
substitution variables only exist while `deploy.sh` runs. Compose refuses to start
without them, and DB/object-store ports stay loopback-only.

Settings of the reserved `control_plane` section the servers read: `pg_dsn`,
`host_aliases`, `api_keys`, `rustfs_*`/`minio_*`, `self_host`, `web_port`,
`ci_actions` (true enables CI retry/cancel), `inspector_limits` (`max_events`,
`max_bytes`, `max_sessions`, `max_total_bytes`, `retention_hours`, `reasoning_role`,
`sensitive_role`, `store_reasoning`) and `llm_proxy` (`max_inflight`,
`queue_wait_seconds`). `control_plane.compose` is the mapping `deploy.sh` renders
for compose itself (e.g. `HERDR_CP_WEB_BIND`, `HERDR_CP_WEB_PUBLIC_PORT`,
`HERDR_CP_PG_PORT`, `GATEWAY_TAILNET_IP`; all eight are required and `fleet config
validate` names any that are missing). `fleet config init` asks for the gateway tailnet
IP, defaulting to `tailscale ip -4`.

The file is bind-mounted as a single file, so the tools that edit it (`fleet join`,
`fleet config token`) write it in place (same inode, after a dated backup); the
containers see the update at once.

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

## LLM keys for sessions

Sessions started with `herdr-eng session run --project P -- <cmd>` mint a per-session key at
`POST /api/v1/llm/keys` and revoke it at `/api/v1/llm/keys/<id>/revoke` when the command ends, so
token analytics are per project without hand-sourcing a file. When this stack runs with
`--api-keys`, the host running sessions needs an **operator** key in
`HERDR_ENGINEERING_CONTROL_PLANE_KEY` or `~/.config/herdr-engineering/control-plane-key` (0600).
A key the host failed to revoke is named on stderr (`key_id`); revoke it with the same endpoint.
See the README section "Sessions get the LLM env automatically".

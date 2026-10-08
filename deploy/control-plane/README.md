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

## Usage

```bash
# 1) generate ./.env with randomly generated strong credentials (0600)
./generate-env.sh

# 2) bring the stack up (refuses to start until .env provides the secrets)
docker compose up -d --build

# verify migrations applied
docker compose exec postgres psql -U "$HERDR_CP_PG_USER" -d herdr -c '\dt'

# apply migrations / run one pass from the host (needs asyncpg + reachable db)
herdr-eng control-plane migrate
herdr-eng control-plane worker --once
```

`.env` is generated once, kept on the host, and is git-ignored. Regenerate
fresh credentials anytime with `rm .env && ./generate-env.sh` (this rotates
the DB + object-store passwords).

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
`HERDR_CP_WEB_BIND` in `.env` (default `127.0.0.1` = loopback-only), so it is
reachable from any dev.lan machine but **not** from a plain LAN / `0.0.0.0`:

```bash
# expose on dev.lan: set HERDR_CP_WEB_BIND to this machine's Tailscale IP
HERDR_CP_WEB_BIND="$(tailscale ip -4)" ./generate-env.sh
docker compose up -d --build
# any dev.lan machine:
curl http://dev.lan:8080/api/v1/health          # 200
```

The web container runs with `--allow-open` (explicit acknowledgment that the
Tailnet replaces API keys). **Do not** change `HERDR_CP_WEB_BIND` to `0.0.0.0`
or drop the Tailnet-IP mapping — that would expose an unauthenticated admin API
beyond the tailnet.

**Optional RBAC (Spec §104).** If you still want key auth, run the standalone
server (not this compose service): `herdr-eng control-plane web --api-keys
'viewerkey=viewer,operatorkey=operator'` — viewer = read-only, operator = read +
mutate.

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

On the host the DSN defaults to `postgresql://herdr:herdr@127.0.0.1:5432/herdr`
(port 5432 is exposed by the compose project). Override via the
`control_plane:` block in the config or `HERDR_ENGINEERING_CONFIG`.

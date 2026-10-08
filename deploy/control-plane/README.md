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
docker compose up -d --build

# verify migrations applied
docker compose exec postgres psql -U herdr -d herdr -c '\dt'

# apply migrations / run one pass from the host (needs asyncpg + reachable db)
herdr-eng control-plane migrate
herdr-eng control-plane worker --once
```

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

Map the service to `0.0.0.0:8080:8080` (or expose on the dev.lan gateway) if the
control plane should be reachable from the fabric.

**Auth (RBAC, Spec §104)** — optional. Set `HERDR_CP_API_KEYS` to a comma list
of `key=role` pairs (`viewer` = read-only, `operator` = read + mutate):

```bash
HERDR_CP_API_KEYS="viewerkey=viewer,operatorkey=operator" docker compose up -d
curl -H "X-API-Key: viewerkey" http://127.0.0.1:8080/api/v1/health   # 200
curl -X POST -H "X-API-Key: viewerkey" ... /api/v1/sessions/s/messages  # 403
```

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

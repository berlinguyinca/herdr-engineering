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

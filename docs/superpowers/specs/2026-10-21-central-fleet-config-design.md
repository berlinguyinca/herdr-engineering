# Central fleet configuration

Status: draft for approval, 2026-10-21

## Intent

Hosts are configured today by scattered environment variables (`HERDR_CP_URL`,
`PI_INSPECTOR_FEED`, `HERDR_CP_HOST_ALIASES`, `FABRIC_LLM_UPSTREAM_KEY`, CI
tokens, and about forty more). They drift, differ per service manager, and are
invisible. A session page showed no inspector data only because two env vars
were unset on one host.

We want **one configuration file**, owned by the control plane, served from an
endpoint. A host reads it at startup and registers itself. The operator does
not use environment variables for configuration.

## Goals

- All fleet configuration, secrets included, lives in one file.
- Every host fetches its slice at startup and reports which revision it applied.
- `herdr-eng fleet join` is the only bootstrap: it gives a host the control-plane
  address and a per-host credential.
- Reinstalling elsewhere: if the file does not exist, an interactive setup asks
  for the required fields and writes it.
- The Hosts page shows applied-vs-current revision per host (config drift).

## Non-goals

- Pushing config into running processes. New values apply when a process next
  starts (or a host re-fetches on its own restart).
- Removing the process-level inputs the platform forces on us (for example
  `HERDR_PANE_ID` set by herdr, `XDG_RUNTIME_DIR`). Those are not configuration.
- Nothing is out of scope on the file side: Postgres/MinIO credentials and the
  compose settings (bind address, public port, image tag) are in the file too.

## Anti-goals

- A secret reaching a host that has no role needing it.
- A secret in git, in logs, in the Hosts page, or in an error message.
- A bad edit taking the fleet down.

## Decisions (made with the operator)

- Bootstrap through `herdr-eng fleet join`, which writes a small local file
  `~/.config/herdr-engineering/host.json` (control-plane address, host id,
  per-host credential). This replaces env vars as the way a host finds the
  control plane.
- The central file lives **on the control-plane host, outside git**, mode 0600,
  at `~/.config/herdr-engineering/fleet-config.yaml`. The repo holds only a
  schema and a commented example with `REDACTED` placeholders.
- Secrets are included in the file, but each section is tagged with the roles
  allowed to read it; the endpoint serves a host only the sections its roles
  allow.
- Missing file on (re)install: the setup command asks questions and writes it.

## Design (my calls, marked)

**File shape.** YAML, `version` integer, `revision` (content hash, computed, not
authored), `sections:` each with `roles: [...]` and `values: {...}`. A
`hosts:` block maps host id to roles and optional overrides. *(my call)*

**Endpoint.** `GET /api/v1/fleet/config` with `Authorization` carrying the host
credential. Returns `{revision, values}` merged for that host's roles, with
`Cache-Control: no-store`. `POST /api/v1/fleet/config/applied`
`{host_id, revision}` records what a host applied. Unknown or revoked
credential: 401, no detail. *(my call)*

**Credentials.** `fleet join` creates a random per-host token; the file stores
only its hash; the host stores the token in `host.json` (0600). Revoking one
host is one edit. *(my call)*

**Validation.** The control plane validates the file against the schema on load
and on `herdr-eng fleet config validate`; an invalid edit is rejected and the
last good revision keeps serving. Dated local backups are kept on each accepted
change. *(my call)*

**Consumers.** The cp-sessions plugin, the ownership feed, and pi-engineering
(inspector feed) read `host.json`, fetch config, and fall back to the last
fetched copy cached on disk (0600) when the control plane is unreachable, so a
brief outage does not disable a host. The control-plane web container reads the
file itself. **No environment-variable configuration remains**
(decided 2026-10-21: removed in the same release, no deprecated override).
A host that cannot reach the control plane at startup uses its cached copy;
with neither, it refuses to start and says why. Operator decision.

**Compose bootstrap.** Docker Compose reads its variables before any of our
code runs, so `deploy/control-plane/deploy.sh` renders a transient 0600 env file
from the central file (`herdr-eng fleet config render-compose`), hands it to
compose, and deletes it afterwards. The operator never writes or maintains it;
the central file is the only source. The `.env`/`control-plane.env` files and
`generate-env.sh` are retired (generation moves into `fleet config init`).
*(my call)*

**Setup on demand.** `herdr-eng fleet config init` prompts for required fields
(public URL, LLM upstream address and key, CI address and token, host list and
roles), writes the file 0600, and runs validation. Non-interactive when all
values are supplied via a file argument. *(my call)*

**Observability.** `/api/v1/fleet/config/status` (no values) lists current
revision and each host's applied revision; the Hosts page shows a drift badge.

## Left to the builder

Exact schema field names, YAML library choice, the pi-engineering change (a
separate repo; ships as its own PR), and the migration order of consumers.

## Slices

1. Schema, loader, validation, endpoint, join credentials, status API.
2. Consumers: plugin and feed read `host.json`; control plane reads the file.
3. pi-engineering reads central config (enables the inspector feed from it).
4. `fleet config init`, compose render in deploy.sh, Hosts-page drift badge,
   removal of every config env var.

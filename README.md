# HerdR Engineering

**Repository:** `berlinguyinca/herdr-engineering`  
**Product:** Super HerdR — a distributed, mobile-capable engineering cockpit built on top of Herdr rather than a fork of it.  
**Status:** master implementation specification; greenfield repository, integrating existing tools and upstream plugins.

> This repository name intentionally normalizes the earlier draft spellings `berlinguyicna/herdr-engeneering` and the superseded planned repo `berlinguyinca/herdr-powerpack`.

## Why this exists

Running one coding agent in one terminal is easy. Running dozens of agents, worktrees, dev servers, tests, reviews, and CI jobs across several machines is not.

The lab needs one coherent place to answer:

- Which machine is each agent on?
- What is it doing right now?
- Which repository, branch, and worktree does it own?
- Which agents are blocked, idle, done, or still working?
- What did a session start with, what has it completed, and what is next?
- Which local/dev web server belongs to which worktree, and how can I open it from my laptop, phone, or iPad without remembering hostnames?
- Which tests passed or failed, and what is the trace for one test?
- What is happening in Woodpecker/Pileated CI and its queue?
- How do I compare several agents' candidate implementations side-by-side?
- Where are the generated screenshots, reports, plans, reviews, and handoff artifacts?

Herdr already solves the most important runtime problem: persistent real terminal sessions with agent awareness, workspaces, worktrees, and multi-machine SSH connectivity. HerdR Engineering adds the integration layer around it so the whole environment feels like one distributed engineering IDE.

## What HerdR Engineering is

HerdR Engineering is a **thin, curated integration/distribution layer**. It owns configuration, adapters, schemas, UI integration, deployment, health checks, and the few capabilities that do not already exist upstream.

It provides, in implementation order:

1. reproducible Herdr/Pi/engineering setup on the fleet;
2. a pinned, audited Powerpack-style plugin bundle;
3. private web/mobile access through Tailscale;
4. browser/dev-preview and Plannotator integration;
5. a shared artifact/handoff workspace;
6. per-agent session history and newest-first activity timelines;
7. semantic integration with Pi Engineering and AutoSpec;
8. a transparent `dev:<port>` fabric for ephemeral web servers across all machines;
9. an IDE-style cross-language test explorer;
10. Woodpecker/Pileated CI status, logs, queues, and runner visibility;
11. worktree/candidate comparison across many concurrent agents;
12. notifications, approvals, health, security, updates, rollback, and full fleet validation.

## What it is not

HerdR Engineering does **not** become another scheduler, CI engine, model router, source-control system, review engine, or agent runtime.

Ownership is intentionally strict:

| System | Owns |
|---|---|
| **Herdr** | persistent terminals, panes/tabs/workspaces, native worktrees, saved SSH machines, agent state, socket/plugin APIs |
| **HerdR Engineering** | curated distribution, adapters, session/activity UX, dev-service fabric, test/CI views, integration and fleet operations |
| **Pi Engineering** | missions, worker/subagent orchestration, engineering lifecycle, policy, review/repair coordination |
| **AutoSpec** | specs → issues → implementation workflow |
| **Tern** (formerly InferWeave) | model/runtime/GPU routing, capacity, queueing, inference; endpoint `https://llm.metabolomics.us` |
| **Tailscale** | private network, identity-aware reachability, stable service names |
| **Woodpecker / Pileated CI** | pipeline execution and runner queues |
| **Plannotator** | review semantics, comments, approvals |
| **GitHub** | repositories, issues, pull requests, repository history |
| **Pi-Web** | existing Pi-specific operator UI; link/embed where useful, never reimplement |

Pi Forge is explicitly excluded.

## Core design principles

- **Reuse first.** Audit the current Herdr ecosystem before writing replacement code.
- **Native Herdr first.** Use saved machines, `--machine`, worktrees, integrations, events, and plugin APIs instead of recreating them.
- **One source of truth per concern.** Session/activity views observe and correlate work; they do not create a second mission/task engine.
- **Private by default.** Web UIs, dev servers, browser debug endpoints, and internal services stay on Tailscale/private interfaces.
- **Worktrees are local source truth.** Shared storage is for artifacts/handoffs, never a multi-writer replacement for Git worktrees.
- **Observable activity only.** Never record or display hidden chain-of-thought. Store user-visible summaries, tool actions, tests, Git changes, artifacts, and status.
- **Structured signals before terminal scraping.** Prefer native APIs, test protocols, CI APIs, and Pi semantic events.
- **Incremental implementation.** Every numbered spec has entry/exit criteria and leaves the system usable.
- **Recoverable changes.** Pinned dependencies, safe config merging, health checks, backups, canaries, and rollback are mandatory.

## Current validation fleet

The initial fleet is:

- `fry` (not currently on the tailnet)
- `beast` — converged, idempotent
- `bender` — converged, idempotent
- `mac` (renamed from `macbook-m4` to match the real tailnet name) — converged, idempotent

These names are validation inventory, not hard-coded application logic. Future machines must be addable without code changes.

All four should have:

- Herdr
- Pi configured to use `https://llm.metabolomics.us` for all LLM requests
- `berlinguyinca/pi-engineering`
- `berlinguyinca/autospec`
- `vim`
- `mc`
- `btop`

Fleet setup is managed declaratively with Ansible. Tailscale provides private connectivity.

## User experience target

From a laptop, phone, or iPad on the tailnet, the operator should be able to:

- see every connected machine and agent;
- jump directly to a session requiring attention;
- inspect the session timeline and current work;
- open its worktree files and artifacts;
- open its browser/dev server at a stable `dev:<port>` address without knowing the physical host;
- inspect test trees and traces;
- inspect CI pipelines/queues/runners;
- compare candidate worktrees and previews;
- send input, approve/reject review work, and resume a session;
- disconnect without killing remote work.

The physical location of the agent should be visible when useful but should not dictate the normal workflow.

## Repository layout

```text
herdr-engineering/
├── README.md
├── MASTER_SPEC.md
├── START_HERE.md
├── IMPLEMENTATION_PROMPT.md
├── CONTRIBUTING.md
├── SECURITY.md
├── SPEC_MANIFEST.yaml
├── STATUS.md
├── docs/
│   ├── architecture.md
│   ├── decision-history.md
│   ├── upstream-baseline-2026-09-23.md
│   ├── acceptance-matrix.md
│   ├── implementation-roadmap.md
│   ├── spec-genealogy.md
│   ├── github-project-setup.md
│   ├── adr/
│   ├── contracts/
│   ├── specs/                    # normative ordered implementation specs
│   └── archive/                  # prior HerdR specs preserved for lineage
├── herdr_engineering/            # Python integration layer (the implementation)
├── ansible/                      # fleet bootstrap playbooks (Linux + macOS)
├── config/                       # safe defaults/examples; never secrets
├── lock/                         # pinned upstream/plugin dependency lock
├── scripts/                      # dev-env / validate / naming dogfood scripts
├── tests/                        # 79 unit/contract/integration tests
├── fixtures/                     # test fixtures
├── docs/evidence/                # real command snapshots per phase
└── .github/                      # issue/PR templates and CI workflow
```

## Install and use (real commands)

The implementation is a small Python package exposing one CLI, `herdr-eng`,
plus the `herdr_engineering.*` library. It layers on top of an existing Herdr
installation (verified against `herdr 0.9.1`).

### One-shot install (assumes Herdr already exists)

`scripts/install.sh` sets everything up on the machine it runs on and does
**not** install, upgrade, or restart Herdr — your running agent sessions are
unaffected. It is idempotent.

```bash
git clone https://github.com/berlinguyinca/herdr-engineering
cd herdr-engineering
scripts/install.sh
```

The installer, in order: verifies Herdr is present; creates a project `.venv`
and installs the package; creates the machine-local state + artifact
directories; writes a private-by-default machine-local config at
`~/.config/herdr-engineering/config.yaml` (preserved if present); enrolls this
machine (detected host + OS) into the private fleet inventory
(`ansible/inventory/hosts.yml`); puts `herdr-eng` on PATH via
`~/.local/bin`; and runs `herdr-eng doctor` to confirm.

### Manual setup (equivalent)

```bash
# 1) create the venv and install the package (dev extras for tests/lint)
scripts/dev-env.sh
source .venv/bin/activate

# 2) run the full validation (lint + schema + config + lock + tests + doctor)
scripts/validate.sh

# 3) health / capability / security checks
herdr-eng doctor --json
herdr-eng capabilities --json

# 4) automatic semantic workspace naming (dogfooded)
herdr-eng name --prompt "Implement transparent dev routing so agents can preview from any device"
# => transparent-dev-routing-agents  (name_source=auto_prompt)
herdr-eng name --issue-number 421 --issue-title "Add test tree with pytest and JVM support" --repo owner/repo
# => 421-test-tree-pytest-jvm       (name_source=auto_issue)
herdr-eng name --spec-id 0110 --spec-title "transparent dev fabric"
# => 0110-transparent-dev-fabric    (name_source=auto_spec)

# 5) Powerpack distribution / lifecycle
herdr-eng powerpack status
herdr-eng powerpack snapshot pre-release-1
herdr-eng powerpack preflight
herdr-eng powerpack rollback

# 6) private web/mobile control surface (binds 127.0.0.1 by default)
herdr-eng web --port 8787
#    ... or, for the persistent gateway deployment on the designated host:
#    scripts/web-serve.sh   # systemd user unit + linger + `tailscale serve --bg 8787`

# 7) other integration surfaces
herdr-eng machines --json
herdr-eng workspaces --json
herdr-eng devfabric list
# lease a port for a running app, then forward it (protocol-transparent TCP).
# Ports match by default: localhost:5173 shows up at dev.lan:5173 (same port),
# falling back to the 18000-28999 window only when the port is already taken.
herdr-eng devfabric register --machine bender --host 127.0.0.1 --port 5173 --label my-app
herdr-eng devfabric serve lease_xxx            # or --bind <tailscale-ip> for other hosts
# ... or run the auto-registration agent so web services that start on this
#     host appear at a stable dev:<port> (and in the gateway web UI) with zero
#     manual steps — a per-host service on every fleet host:
scripts/fabric-agent.sh --gateway http://dev.lan:29999         # Linux (systemd user unit)
scripts/fabric-agent-macos.sh --gateway http://dev.lan:29999   # macOS (launchd LaunchAgent)
# remove a machine from the fleet (stop agent, close leases, drop from inventory):
scripts/fabric-offboard.sh
herdr-eng tests adapters
herdr-eng tests run pytest --repo owner/repo --worktree 0110-dev-fabric --machine localhost
herdr-eng ci repos                          # list CI repositories
herdr-eng ci agents                         # runner/queue state (agent token stripped)
herdr-eng ci pipelines --repo owner/repo    # pipelines with status
herdr-eng ci pipeline --repo owner/repo 12  # detail: workflows -> tasks (state + exit code)
herdr-eng ci debug --repo owner/repo 12     # bounded Debug-with-Pi handoff for failures
herdr-eng candidates id --repo owner/repo --worktree wt-18 --branch checkout-redesign-a
herdr-eng journal timeline <session_id>
herdr-eng attention list
herdr-eng artifacts list
herdr-eng browser http://localhost:5173 --json
```

Run `herdr-eng --help` for the full command set. Every command that emits
structured data supports `--json`. The web UI is private by default
(`private_only: true`, host `127.0.0.1`); expose it only via the tailnet.

### Security model (control plane)

Two deployment modes, each with an honest posture:

**Compose deployment (dev.lan).** The **Tailnet is the trust boundary** — all
machines belong to each other and are encrypted end-to-end via WireGuard, so
access is **auth-free within dev.lan**. Safety comes from *how it is bound*, not
per-user keys:
- The web host port binds to the **Tailnet interface** (`HERDR_CP_WEB_BIND` =
  `tailscale ip -4`), so it is reachable from any dev.lan machine but not from a
  plain LAN / `0.0.0.0`. The container runs `--allow-open` to acknowledge this
  boundary explicitly.
- Postgres / MinIO credentials come from a git-ignored `.env` with **randomly
  generated strong passwords** (`./generate-env.sh`); compose refuses to start
  without them, and DB/object-store ports stay loopback-only.
- Secrets in event payloads/messages are **redacted at the API boundary**
  (never stored or rendered).
- **Do not** set `HERDR_CP_WEB_BIND` to `0.0.0.0` — that would expose an
  unauthenticated admin API beyond the tailnet.

**Standalone server (hardened, not yet production-secure).**
- **Fail-closed by default**: an unauthenticated server refuses to bind any
  non-loopback address; loopback stays open for local dev only.
- **Optional RBAC** via `X-API-Key` (`viewer` = read-only, `operator` = read +
  mutate), verified with a constant-time compare.
- **Before production exposure:** serve over **TLS** (`tailscale serve` gives
  an HTTPS URL), add **rate-limiting** (no brute-force protection today), and
  treat **redaction as best-effort** regex.

### Control plane (spec 0190)

The control plane is a structured `/api/v1` JSON + SSE server with a web UI
that is a *client* of that API. It runs standalone (in-memory, optionally
hydrated from Postgres) or in the `deploy/control-plane` compose project:

```bash
# standalone UI + API, populated with demo data (no Postgres needed)
herdr-eng control-plane web --port 8080 --seed-demo
# then open http://127.0.0.1:8080/ (Overview / Missions / Sessions / Services / Hosts / Activity / Analytics)

# role-based access (X-API-Key): viewer = read-only, operator = read + mutate
herdr-eng control-plane web --port 8080 --api-keys 'view1=viewer,op1=operator'

# hydrate from durable Postgres state on startup (asyncpg; graceful if absent)
herdr-eng control-plane web --port 8080 --pg-dsn 'postgresql://herdr:herdr@127.0.0.1:5432/herdr'

# full compose stack (postgres + rustfs(minio) + worker + web)
#   the UI is served at http://dev.lan (port 80) on the Tailnet interface
cd deploy/control-plane
./generate-env.sh          # writes .env with strong random passwords (idempotent)
docker compose up -d --build
# then open http://dev.lan/
```

Deployed on **bender**, the control-plane stack serves the HerdR UI at
`http://dev.lan` (Tailnet IP `:80`) with durable Postgres + MinIO:

- `HERDR_CP_WEB_BIND=100.104.39.6` (Tailnet interface) + `HERDR_CP_WEB_PUBLIC_PORT=80`.
- `HERDR_CP_PG_DSN` is generated into `.env`; the web reads it from the env (not
  a `--pg-dsn` process arg) so the DB password never appears in `ps`/`docker inspect`.
- Postgres host port defaults to `127.0.0.1:5432`; set `HERDR_CP_PG_PORT` to a
  free loopback port (e.g. `15433`) when 5432 is already taken on the host.
- MinIO is pinned to `minio/minio:RELEASE.2025-09-07T16-13-09Z` (`minio:latest`
  is not always pullable); swap the image for a real RustFS image later —
  config-only.
- The image is stamped with the build commit (visible in `/api/v1/health`):
  build with `docker compose build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD)`.
- **TLS:** traffic inside the Tailnet is already WireGuard-encrypted, so plain
  HTTP on `dev.lan` is fine. To expose beyond the tailnet, terminate TLS with
  `tailscale serve --https=443 http://127.0.0.1:8080` on bender (gives an HTTPS
  URL) — the app itself does not do TLS termination.

#### Repeatable deploys

Deploys go through scripts, not hand-built compose overrides. Secrets stay in a
host-local 0600 env file (`~/.config/herdr-engineering/control-plane.env`, or
`HERDR_CP_ENV_FILE`); nothing secret is committed.

```bash
# control-plane web: build from a ref, recreate only control-plane-web,
# wait for /api/v1/health to report the commit, roll back on failure.
# Writes a receipt to ~/.local/state/herdr-engineering/deploys/<sha>.json
deploy/control-plane/deploy.sh --dry-run            # show what would happen
deploy/control-plane/deploy.sh --ref origin/main

# per-host plugin + ownership feed (hosts listed in
# ~/.config/herdr-engineering/fleet-hosts.txt, see deploy/fleet-hosts.example.txt)
deploy/deploy-fleet-agents.sh --dry-run
deploy/deploy-fleet-agents.sh --ref origin/main [--hosts bender,beast]

# which hosts run different claude/codex/pi/plugin versions (suggestions only)
herdr-eng fleet drift --control-plane http://dev.lan
```

The ownership feed is Linux-only (it reads `/proc`), so launchd hosts get the
plugin but not the feed. Stale `unknown` services can be retired in bulk; this
only records a reversible event (a new report restores the service). Dry-run is
the default:

```bash
curl -XPOST -H 'X-API-Key: $OPERATOR_KEY' -d '{"older_than_days":7,"states":["unknown"]}' \
  http://dev.lan/api/v1/services/retire             # dry run: counts only
curl -XPOST -H 'X-API-Key: $OPERATOR_KEY' -d '{"older_than_days":7,"states":["unknown"],"dry_run":false}' \
  http://dev.lan/api/v1/services/retire
```

Note: the web hydrates from Postgres **once at startup**, so it reflects a
snapshot of durable state; live updates flow over SSE. Restart the web to
re-hydrate after external writes.

The UI subscribes to `/api/v1/events/stream` (SSE) for live updates — no
polling. Secrets in event payloads and messages are redacted at the API
boundary (never stored or rendered).

#### Live-Postgres end-to-end test (opt-in)

The default suite is pure-in-memory (no live Postgres needed in CI). The one
suite that touches a real database is `tests/control_plane/test_e2e_live_db.py`;
it **skips unless** you point it at a throwaway Postgres via `HERDR_E2E_PG_DSN`
(and `asyncpg` is installed). It exercises the full durable round-trip the
deployed stack uses: `Migrator` applies `deploy/control-plane/migrations`, rows
are seeded into `missions`/`sessions`/`services`/`hosts`/`fabric_events`, then
`PostgresSource`+`load_into` hydrate a `ControlPlaneRepo` and a real
`ControlPlaneServer` serves it over `/api/v1` (`/health` reports `source: postgres`).
Teardown drops the schema, so the target database stays reusable and the real
`herdr` DB is never touched.

```bash
# create a dedicated throwaway database (default postgres user is a superuser)
docker exec herdr-cp-postgres psql -U "$HERDR_CP_PG_USER" -d postgres -c 'CREATE DATABASE herdr_e2e'
# point the test at it and run just the live e2e suite
HERDR_E2E_PG_DSN="postgresql://$HERDR_CP_PG_USER:$HERDR_CP_PG_PASSWORD@127.0.0.1:15433/herdr_e2e" \
  .venv/bin/pytest tests/control_plane/test_e2e_live_db.py
```

## Implementation order

`SPEC_MANIFEST.yaml` is the machine-readable source of order and dependencies. `docs/implementation-roadmap.md` visualizes the dependency graph, while `docs/spec-genealogy.md` maps every predecessor HerdR specification into the current program. Humans should start with:

1. `START_HERE.md`
2. `docs/specs/0000-master-program.md`
3. specs `0010` through `0180` in order, respecting dependency gates.

Parallel work is allowed only when the manifest marks specs independent and their dependencies are complete.

## Upgrades and rollback

Updates are safe, previewed, and reversible. Nothing is auto-upgraded.

```bash
herdr-eng powerpack snapshot pre-update      # capture current lock+config
herdr-eng powerpack preflight                # doctor + incompatible-dep check; blocks bad updates
herdr-eng powerpack rollback                 # restore the last known-good snapshot
```

`preflight` returns non-zero (and `ok: false`) when the doctor fails or a
locked dependency is `REJECT`-classified, so a bad update is blocked before it
applies. A failed optional plugin never takes Herdr down: the layer degrades
and `herdr-eng doctor` reports which check failed.

## Plugin policy (reuse-first)

We adopt/adapt upstream Herdr plugins rather than rebuild. Every dependency is
classified and pinned in `lock/upstreams.yaml`; `HERDR_COMPATIBILITY.md` is the
human-readable audit. Decisions: `ADOPT` (Herdr core, herdr-plannotator),
`ADAPT` (herdr-web, herdr-browser, Pi-Web), `OPTIONAL` (swarm, worktree
include, gh-checks, pr-board, notifications), `REJECT` (Pi Forge, any duplicate
of a native primitive). A new dependency enters the Powerpack only after being
re-verified against the installed Herdr version (see `HERDR_COMPATIBILITY.md`
refresh rule).

## Security model

- **Private by default.** Web binds `127.0.0.1`; dev-fabric refuses public bind
  addresses; nothing listens on the public internet unless you explicitly opt
  in.
- **No secrets in Git.** Inventories, auth keys, and credentials are gitignored
  and live outside the repo. The installer never writes a secret anywhere.
- **Audited.** `herdr-eng doctor` reports public listeners, config/lock
  validity, and capability state in human + `--json` form.
- **No hidden reasoning persisted.** Only user-visible summaries, tool actions,
  test/CI results, Git changes, and artifacts are stored.

See `SECURITY.md` for the reporting policy.

## Platforms

Linux (Debian/Ubuntu) and macOS (Homebrew) are the supported fleet targets;
the Ansible roles and the installer both branch on `uname`. The Python layer
runs on 3.11+. Validation evidence to date is from a Linux host (`bender`);
macOS convergence is a documented `BLOCKED_EXTERNAL` item pending a fleet run.

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| `herdr-eng: command not found` | `source .venv/bin/activate` or re-run `scripts/install.sh` (symlinks into `~/.local/bin`) |
| doctor: `tailscale` warn | `tailscale status`; if down, `sudo tailscale up` (key via `TS_AUTH_KEY`, see installer) |
| doctor: `public listener` warn | `herdr-eng doctor --json` lists it; stop the offending process or scope it to the tailnet |
| web UI not reachable from phone | confirm `tailscale up` on both, then open `http://<tailnet-ip>:8787` (web is private-only) |
| dev service not forwarding | `herdr-eng devfabric list` (lease active?) then run `herdr-eng devfabric serve <lease>` |
| config not taking effect | `herdr-eng doctor --json` shows the config file in use; precedence is machine-local < repo < `$HERDR_ENGINEERING_CONFIG` |
| stale dev port | `herdr-eng devfabric reconcile` expires stale leases |
| `ci` says offline / stale | set the operator token: `~/.config/herdr-engineering/ci-token` (chmod 600) or `HERDR_ENGINEERING_CI_TOKEN`; the token is a secret and never lives in the repo. Without it the view is explicitly stale, never guessed |
| `ci logs` returns no log text | expected on this Woodpecker 3.x instance: logs stream over WebSocket (no REST endpoint) and the OAuth2 proxy doesn't forward a bearer token on the upgrade. Use the `view_in_web_ui` link it prints; `ci debug` still gives the failing tasks + exit codes |
| `dev:<port>` unreachable from other hosts | is the gateway up? `herdr-eng devfabric gateway` on the gateway host; point commands at it with `HERDR_ENGINEERING_FABRIC_URL=http://<gateway>:29999` (or `fabric.gateway_url` in config); the app must be bound to the tailnet interface or `0.0.0.0` |

## Dev fabric fleet lifecycle (multi-operator)

Any operator of this repo can stand up a private dev net and add/remove
machines with a couple of idempotent scripts: `scripts/fabric-stack.sh`
(deploy the master-host CoreDNS + gateway + web stack), `scripts/fabric-agent.sh`
/ `scripts/fabric-agent-macos.sh` (join a Linux / macOS machine), and
`scripts/fabric-offboard.sh` (leave the fleet). Full walkthrough — set up your
dev net, join a machine, verify, offboard — plus the one external Tailscale
admin-console split-DNS step:
[`docs/operations/fleet-lifecycle.md`](docs/operations/fleet-lifecycle.md).

## Dev fabric gateway (unified `dev:<port>`)

One gateway host (bender today) runs `herdr-eng devfabric gateway`: it owns
the shared lease registry, a dynamic router (each leased external port bound
on loopback + tailnet, byte-proxied to the owning machine), and a probe loop
that expires dead apps and releases their ports. Every lease then gets a
stable fleet-wide address — `http://<gateway-magicdns>:<port>` — reachable
from any tailnet device (desktop, phone, iPad) without knowing the physical
host. Registrar commands (`register`/`heartbeat`/`close`/`list`) use the
gateway automatically when configured (`fabric.gateway_url` or
`HERDR_ENGINEERING_FABRIC_URL`) and degrade to local mode with an explicit
warning when it is not reachable. The control API is tailnet+loopback only
and bearer-token protected when a token is configured. Design and live
evidence: [`docs/architecture/dev-fabric-gateway.md`](docs/architecture/dev-fabric-gateway.md).

## Persistent private web UI (gateway)

`herdr-eng web` (spec 0060) is the fleet-wide private control surface. It runs
**persistently on ONE designated gateway host** (bender today) and is exposed
tailnet-only. The model mirrors the dev-fabric gateway:

- **Private-by-default invariant.** `web.py` binds loopback only and its
  defaults are **not** changed. The systemd user unit pins
  `--host 127.0.0.1` so a machine-local config can never turn it into a public
  listener. There is no public listener, ever.
- **One gateway host.** Only the designated host runs the service. Other fleet
  hosts reach the UI via the stable `https://<gateway-magicdns>` URL or an SSH
  tunnel (`ssh -L 8787:127.0.0.1:8787 <gateway>`), never via a public listener.
- **Persistent.** A systemd **user** unit (`scripts/herdr-eng-web.service`)
  keeps it running across reboots/logout (linger enabled).
- **Tailscale serve.** `tailscale serve --bg 8787` maps
  `https://<gateway-magicdns>` → `127.0.0.1:8787`. The serve feature must be
  enabled once on the tailnet (admin console); until then the setup script
  prints the exact command and continues gracefully.

```bash
# on the gateway host (bender), from a checkout:
scripts/web-serve.sh   # idempotent: installs+starts the user unit, enables
                       # linger, then `tailscale serve --bg 8787`
```

Ansible deploys the same unit + script on the designated gateway host
(`ansible/playbooks/site.yml`, gated by `herdr_engineering_gateway_host`).
Design and deployment model:
[`docs/architecture/dev-fabric-gateway.md`](docs/architecture/dev-fabric-gateway.md).

## Contributing

See `CONTRIBUTING.md`. In short: run `scripts/validate.sh` (lint + tests +
docs + config + lock), keep the acceptance matrix honest (mark
`BLOCKED_EXTERNAL` with evidence rather than guessing), and never commit
secrets or hidden chain-of-thought.

## Definition of done

The project is complete when a fresh supported Linux or macOS machine can be enrolled declaratively, the four-machine validation fleet passes the acceptance matrix, mobile/tablet access works privately, concurrent agent worktrees can publish and compare dev previews, local and CI test evidence is navigable, session activity survives reconnect/restart, and all security/update/rollback drills pass.

The final release must not depend on undocumented manual SSH edits or one-off local configuration.

## Fabric LLM gateway (project keys, token accounting, custom models)

`herdr-llm-proxy` (compose service `llm-proxy`, port 8088 on the tailnet interface) is an
OpenAI-compatible endpoint for fabric agents. Each project gets its own key:

```
curl -X POST http://dev.lan/api/v1/llm/keys -d '{"project":"my-project","label":"ci"}'   # secret shown once
curl -X POST http://dev.lan/api/v1/llm/keys/<key_id>/revoke
export OPENAI_BASE_URL=http://dev.lan:8088/v1 OPENAI_API_KEY=hk-...
```

Requests are forwarded to `https://llm.metabolomics.us` with the one dedicated lab key
(`FABRIC_LLM_UPSTREAM_KEY` in `deploy/control-plane/.env`, never given to agents) and the token
usage (including streaming) is recorded against the project: `GET /api/v1/analytics/tokens`
(`?project=`, `?since=`) and the **Token usage** page at `#/analytics/tokens`. Custom models
(`POST /api/v1/llm/models {"name","base_url","upstream_model"}`) are routed to their own endpoint
and never receive the lab key. The gateway is not exposed beyond the fabric. It uses the public
HTTPS API only; the `upstream` link role (specs repo, spec 63) will replace this path.

**Gateway identity and backpressure.** All projects share the one lab key, so the proxy tells the
gateway who is calling: `X-InferWeave-Client-ID` is always `herdr:<project>`, and
`X-InferWeave-Session-ID` / `X-InferWeave-Agent-ID` (or `X-Session-Id`) pass through when they match
`[A-Za-z0-9-_.:/]{1,128}` (the session falls back to `<project>:<key_id>`). Without this every request
shared the gateway's single default session/agent bucket (4 active, 12 queued). Each project is also
capped at `HERDR_LLM_PROXY_MAX_INFLIGHT` concurrent requests (default 32, `0` disables); a request that
cannot get a slot within `HERDR_LLM_PROXY_QUEUE_WAIT_SECONDS` (default 10) gets `429` with
`Retry-After` and `error.type: proxy_backpressure`, without reaching the gateway.

A revoked key stops working within 10 s (the proxy reloads its key table every 10 s; an unknown key forces a reload at most every 2 s).

Mint a key and an env file for a project (the key is never printed):
`herdr-eng llm-env --project my-project` writes `~/.config/herdr-engineering/llm-my-project.env`
(0600, `OPENAI_BASE_URL`, `OPENAI_API_KEY`); `source` it before starting an agent.

### Sessions get the LLM env automatically

`herdr-eng session run --project my-project [--session S] [--mission M] -- <command...>` starts
`<command>` (an agent, a shell, a mission runner) with, in **its environment only**:

| Variable | Value |
| --- | --- |
| `OPENAI_BASE_URL` | `llm.gateway_url` (default `http://dev.lan:8088/v1`) |
| `OPENAI_API_KEY` | a key for the project, minted through `POST /api/v1/llm/keys` |
| `HERDR_PROJECT`, `HERDR_SESSION_ID`, `HERDR_MISSION_ID` | context (non-secret) |
| `HERDR_ENGINEERING_FABRIC_URL`, `HERDR_ENGINEERING_CONTROL_PLANE_URL`, `HERDR_FABRIC_MACHINE` | service-URL hint: the gateway to call `/resolve-loopback` and `/rewrite` on, and this machine's name for `from_machine` |

Token usage is then attributed to the project with no manual sourcing. Key lifecycle:
`llm.key_mode: session` (default) mints one key per session labelled `session:<id>` and revokes it
when the command exits, however it exits; `project` mode reuses a cached project key
(`~/.config/herdr-engineering/llm-<project>.env`, 0600, ignored if more permissive) and re-mints it
if it was revoked. The key never appears in argv, logs, stdout or error text. If the control plane is
unreachable the command still starts, with a warning (`--require-llm-env` makes that an error).
Opt out with `--no-llm-env`, `llm: {inject: false}` in config or `HERDR_ENGINEERING_LLM_INJECT=0`.
When the control plane runs with API keys, give the operator key via
`HERDR_ENGINEERING_CONTROL_PLANE_KEY` or `~/.config/herdr-engineering/control-plane-key` (0600).
The session's own processes (and root) can read the key from its environment; that is the
trade-off for tracking, and the reason a session key is short-lived. A wrapper killed with
SIGKILL cannot revoke; its key stays labelled `session:<id>` in `GET /api/v1/llm/keys` for cleanup.

The auto-registration agent (`herdr-eng devfabric agent`) logs one warning per port that listens
like a dev service but holds no lease (non-HTTP, or registration failed), and `scan_once` reports
them under `unleased`; such services are not URL-rewritten.

### Optional header rewriting for HTTP leases

`herdr-eng dev gateway --rewrite-headers` (off by default) rewrites loopback URLs
(`http://localhost:<port>`) in the `Location`, `Access-Control-Allow-Origin`, `Content-Location`
and cookie `Domain` headers of HTTP leases to the fabric address. It forces `Connection: close`
on those leases so every response passes the filter; bodies (which may hold signed or
script-built URLs) and websocket upgrades are never touched.

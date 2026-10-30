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
#    docs/operations/central-config-cutover.md). One deploy runs at a time
#    (deploys/deploy.lock, --deploy-lock-wait); with collab it also holds collab-sync's
#    lock (--collab-lock-wait) and recreates collab-public exactly once. A service
#    container found missing is recreated, also by a rollback.
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
`ci_actions` (true enables CI retry/cancel), `ci_log_tests` (test counts read from finished runs' CI logs; on by default, see `docs/ci-test-reports.md`), `deployment_definitions` (absolute path, as the
web container sees it, to a checkout of InferWeave's `deploy/pipelines`; read live and validated with
the same rules as its `validate.py`. Absent: no definitions, any host may report any component and the
Deployments snapshot warns. Set: a host may report only components whose `host` is its fleet id, and
an invalid set keeps the last good one, or refuses events with 503 if none was ever valid; see
"Cross-track order" for the optional `requires` key),
`deployment_actions` (exactly `true` enables the audited Deployments actions; read live, and off while the
file is unusable unless `config_failure_mode: last_good` — see "Deployment actions" below), `inspector_limits` (`max_events`,
`max_bytes`, `max_sessions`, `max_total_bytes`, `retention_hours`, `reasoning_role`,
`sensitive_role`, `store_reasoning`), `inspector_snapshots` (`enabled`, default off;
`interval_seconds`, default 600, at least 60: the worker keeps a replay snapshot of the inspector so a boot
replays only newer events instead of every stored one; see "Inspector snapshots" below), `artifact_reconcile` (`enabled`, default off; the **worker** runs the artifact registry's idempotent repair pass every `interval_seconds`, default 300, at least 60: orphan blobs are marked after `orphan_ttl_hours` (default 24) and never deleted, expired uploads are closed, tombstones past their hold window are purged; `grace_seconds` default 60, `limit` default 100; it does not re-emit lineage, the worker has no graph emitter) and `llm_proxy` (`max_inflight`,
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

## Deployment definitions (`deploy/pipelines` sync)

InferWeave owns the pipeline definitions (`deploy/pipelines/<component>.yaml` in `InferWeave/inferweave`).
`control-plane-web`, their only reader, sees them read-only at `/etc/herdr/pipelines/`, bind-mounted from
`~/.local/share/herdr-engineering/pipelines/` on the deploy host. Turn them on with one line under
`sections.control_plane.values` of the central fleet file:

```yaml
sections:
  control_plane:
    values:
      deployment_definitions: "/etc/herdr/pipelines"
```

The key is read live (no redeploy). A set loads all-or-nothing; an invalid one keeps the last good set in use.

**Order matters:** deploy first, wait for the first sync (`pipelines.commit` exists and the directory lists
the definitions), and only then add the line. The loader treats an EMPTY directory as a valid set of zero
definitions: no warning, yet every host's deployment events are refused because no component is defined.
deploy.sh creates the directory empty, and the sync never empties it (it refuses an empty set).

- **deploy.sh** creates the directory empty (0755) on a real deploy, never on `--dry-run`, and refuses to deploy
  unless it is a real directory owned by the deploying user, not writable by group/other, holding only regular
  `*.yaml` files (no symlinks or subdirectories, at most 256, each at most 64 KiB, none group/other-writable).
  Every real deploy installs (and a failed one restores) `pipelines-sync.sh`, a copy of the validator
  `herdr_engineering/control_plane/deployment_definitions.py` from the deployed commit, and the user units
  `herdr-pipelines-sync.service` + `.timer`.
- **The timer** runs `pipelines-sync.sh once` about 20 s after it starts and then every 5 minutes. Runs never
  overlap (`flock` on `pipelines.lock`); git calls are bounded with `timeout`, the unit with `TimeoutStartSec`.
  Each run fetches `main` into a dedicated bare, blob-less clone
  (`~/.local/share/herdr-engineering/inferweave-pipelines.git`; it never has a working tree, and only the
  blobs of `deploy/pipelines` are downloaded). It takes ONLY the regular `*.yaml` files directly under
  `deploy/pipelines/` (symlinks, subdirectories, `validate.py` and other files are skipped) and validates them
  with herdr's own `validate_dir` in a staging directory next to the mount. Only a set that passes replaces
  the mounted files: changed files are renamed in one at a time, then removed definitions are deleted. A failed
  fetch, a file over 64 KiB, more than 256 files, an empty set or any validation error keeps the previous set
  and fails the unit (see the journal). The mounted directory itself is never replaced, because a bind mount
  pins its inode. The commit in use is written to `~/.local/share/herdr-engineering/pipelines.commit`.
- **Authentication** is ssh as the deploying user: the remote is `git@github.com:InferWeave/inferweave.git`,
  with `BatchMode=yes` and no prompts. No token is used or stored. The user's ssh key must be able to read the
  repository without an agent or passphrase prompt (the timer has neither), and `github.com` must be in
  `~/.ssh/known_hosts`. The lab `~/.gitconfig` rewrites https GitHub URLs to ssh, so an https remote ends up
  on ssh as well. User timers run while the user is logged in unless lingering is on
  (`loginctl enable-linger`).

Verify after a deploy:

```bash
systemctl --user start herdr-pipelines-sync.service       # or wait for the timer
journalctl --user -u herdr-pipelines-sync -n 20            # "pipelines: <commit> N definitions, ..."
ls -l ~/.local/share/herdr-engineering/pipelines/          # only *.yaml, 0644
docker exec herdr-cp-web ls /etc/herdr/pipelines           # the same files, read-only
```

On the **Deployments** page, the definitions status must say configured and loaded, with no warning, AND the
component groups must list the definitions (an empty list with no warning is the empty-directory case above).
`no deployment definitions loaded` means the key is not set. `configured but invalid` means no set was ever
valid. `have errors: the last good set is still in use` means the current files fail and the page lists the
errors. The component groups (stages) come from the definitions. The same status is in
`GET /api/v1/deployments` under `definitions` (`configured`, `loaded`, `errors`, `warning`, and a non-empty
`groups`).

Run once by hand (same flags as the unit's `ExecStart`): `~/.local/share/herdr-engineering/pipelines-sync.sh once`.
To roll back, drop `deployment_definitions` from the fleet file (the key is read live), then
`systemctl --user disable --now herdr-pipelines-sync.timer`.

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
    hosts. A listed name is compared with the whole `Host` header, so list it as `host:port` (for example
    `herdr.example:8443`) whenever the browser reaches the dashboard on a non-default port.
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
- **Release evidence comes from GitHub.** herdr reads it with the server-held GitHub credential (read only is
  enough; the repository is private, so a credential is required): the `github.app` installation token of the
  repository's owner when the App is configured (see "GitHub App authentication" below), else `github.token`:
  - **Published:** the `<follows.asset>.commit` asset of the `follows.tag` release (`node-ci-latest`) must hold
    the plan target. Every (tag, asset) a track's definitions name must carry the same 40-hex commit. The asset's
    redirect is followed by hand, without the token, and only to a `*.githubusercontent.com` download host.
  - **On main:** `compare/main...<target>` must answer `identical`, or `behind` with no commits of its own, and
    the target must not be behind the plan commit (`compare/<target>...<plan>` not `ahead`). A plan commit that
    was force-pushed away (`diverged`) does not halt a target that is on main.
  - **CI green** is not fetched again. The plan target is only ever a commit whose whole CI pipeline herdr saw
    succeed on a main push, which is stricter than the `follows.ci_context` status alone.
  - **Never in the way.** The coordinator reads a per-target cache and never waits on GitHub: a daemon worker
    refreshes it. A success is fresh for 60 s and served at most 180 s after it was fetched. A failed refresh
    retries after 15 s and never extends a success. Each call is bounded to 10 s wall clock, and responses are
    size-capped and validated. A new target is therefore unknown for one tick (30 s) before it can be confirmed.
  - **Rate limits.** A 403/429 rate-limit answer pauses every refresh (one token) for its `Retry-After`, or until
    `X-RateLimit-Reset`, at least 15 s and at most 15 min. The release and compare reads send `If-None-Match`; a
    304 re-validates the body stored with that ETag, and a 304 with no stored body is a failure, so a 304 never
    confirms anything a 200 did not. About 4–5 calls per track per minute.
  - **Fail closed.** No token, any HTTP error, a timeout, an oversized or malformed answer: the evidence is unknown
    and the coordinator holds with "release not confirmed". Every permit then answers `permitted: false`, and a
    host updater that has a gate URL (`IW_DEPLOY_GATE_URL`) stays on its running version. The Deployments view
    shows each plan's `evidence_status` (`evidence`: `configured` after that track's last refresh succeeded, else
    `unknown`; `evidence_error`: a fixed failure kind), and `coordinator.evidence` over all tracks (`configured`
    only when every track's is). The token is never logged or shown.
  - **The host still verifies the bytes.** A permit names a commit, not a file: the host updater must check that
    the binary it downloads is that permitted commit (its `.commit` asset and the embedded revision, plus the
    `.sha256`) and refuse it otherwise, as `iw-node-pull-deploy` does.
- **The permit gate is not behind the flag.**
  - Nothing is permitted unless the newest tick confirmed the release (published commit matches the target)
    AND the target on main. Not the plan, not a force, and not a repair other than a rollback to herdr's
    recorded previous good commit.
  - A component is permitted when the newest coordinator tick (at most 90 s old, not stale) lists it in
    `permitted` and the commit is the plan target.
  - A pending force for that component and target also permits it, but only where staging holds the plan: a Start
    whose turn has not come, the hold timer, or a halt of the component's OWN stage. A halt in an earlier stage still
    blocks a later stage's force. That tick must also have the release confirmed and the target on main.
  - A force never overrides "release not confirmed", "target no longer on main", an earlier-release or
    unknown-ancestry hold, or a coordinator error.
  - A replan onto another commit voids a pending force.
  - The host sequence is: list requests, ask permit (`forced: true`), `DELETE` the request, redeploy. The
    `DELETE` re-checks the flag (403) and the permit (409, e.g. after a replan or once the release is no longer
    confirmed): only a 200 means "redeploy".
  - Host routes are rate limited per host. An unusable fleet file answers 503, not 500.
  - The durable writes made under a store lock (a force pickup, an escalation round) time out after 10 s and
    then fail closed.
- **Turning the flag off** is the kill switch: pending forces are ignored by the gate and the request list is
  empty. The plan's own permission is unaffected.
- **While the fleet file is unusable** (deleted or unparseable after having been valid), the flag reads as off
  (fail closed) unless the last good file set `control_plane.config_failure_mode: last_good`. With `last_good`, a
  flag that was `true` keeps forces live: new force/ack requests are accepted and pending forces still permit.

### GitHub App authentication (`github.app`)

Every herdr GitHub **reader** (the release evidence above and the GitHub issue/project cache) can authenticate
as a read-only GitHub App instead of the static `github.token`:

```yaml
github:
  roles: [ci]
  values:
    app:
      app_id: 123456
      private_key_file: "/etc/herdr/github-app/github-app.pem"   # absolute, regular file, mode 0600
      installations:                                        # one per repository owner herdr reads
        InferWeave: 11111111
        metabolomics-us: 22222222
        berlinguyinca: auto                                 # looked up once via GET /app/installations
```

- **Required App permissions.** Repository: **Contents: read** (releases, release assets, compare),
  **Metadata: read**, **Issues: read** (the GitHub cache). Organization: **Projects: read** only if the cache's
  Projects v2 sync is used (GitHub may not expose a personal account's projects to an App; such an owner's
  projects then read as unavailable, fail closed). **Commit statuses, Checks and Actions are not needed**: no herdr reader fetches them (CI green
  comes from herdr's own CI record). Every token request names its permissions explicitly and only at `read`:
  evidence `{contents, metadata}`, cache `{issues, metadata}`, Projects `{issues, metadata, organization_projects}`.
  A grant broader than `read` in GitHub's answer is refused, and herdr can request nothing outside those four
  permissions. The evidence token is further limited to the one repository it reads (`repositories: [<name>]`).
- **Precedence.** With `app` set the App wins for every read and one warning is logged when `token` is also set.
  An invalid `app` block means *no* credential (fail closed), never a silent fallback to `token`. Without `app`,
  `github.token` works exactly as before. `deployment_escalation.issues` (an issue **write**) uses
  `github.token` unless the `escalations` write capability is enabled (below); reads never get write access. While `github.token` is configured it stays the fallback, so to stop escalation writes remove both the capability and `github.token`; each filed issue records which credential made it.
- **Owners.** A request uses the installation of the repository's owner. Only owners listed under
  `installations` get a token (`auto` resolves an id, it never adds an owner); any other owner reads as unknown.
  **Keep the App private** (installable only by its owning account): `auto` matches installations by account
  login, so a public App installed by other accounts must never be relied on; prefer explicit ids.
- **Tokens.** A RS256 JWT (`iat` backdated 60 s, 10 min span) is exchanged at
  `POST /app/installations/{id}/access_tokens`; the token is cached per owner and permission set until 5 min
  before it expires (validity anchored to the monotonic clock, capped at 1 h). Refreshes are single-flight, each
  call is bounded to 10 s, redirects are never followed, the answer is validated (`ghs_` token, `expires_at`),
  a 403/429 rate limit backs off for its reset (at most 15 min) and any other failure for 30 s. A 401 from a read
  drops that owner's cached token. Any failure means no token: evidence is unknown, the cache reports GitHub
  unavailable. No HTTP proxy is used (environment proxies are ignored), so no proxy sees the JWT or a token.
- **Secrets.** The key is read from the file only when a JWT is minted. It is refused unless it is a regular
  file of mode exactly 0600 or 0400, with no symlink anywhere in its path, owned by the reading process's uid;
  root (the container under rootful docker) may read the key that keeps its host owner. The key, the JWT and installation tokens never reach a log, an error, the state or the UI. Both
  readers run in `control-plane-web`, which alone mounts `~/.config/herdr-engineering/github-app/` read-only at
  `/etc/herdr/github-app/` (the fleet directory may hold only `fleet-config.yaml`). `deploy.sh` refuses to deploy
  unless that directory is a real 0700 directory owned by the deploying user holding nothing or exactly one
  regular file `github-app.pem`, owned by that user, mode 0600 or 0400; a missing directory is
  created empty. Like the fleet file, the key stays owner-only: the container runs as root (rootless docker: as
  the deploying user), so it can read it; never loosen the mode. Needs `PyJWT[crypto]` (the image's `review-gate`/`collab` extras
  already install it; without it every token fails as `jwt_library_missing`). The loader validates the block's structure (`herdr-eng fleet config validate`), not the
  key file, which only the control-plane container can see.
- **Write capabilities (`write: [...]`, off by default).** Reads stay read-only. A deployment may list, under
  `github.app.write`, the write capabilities it allows; each has a fixed permission set that no caller can widen, and a
  capability that is not listed is refused (`write_disabled`) before any call to GitHub:

  | Capability | Used by | App permissions needed |
  |---|---|---|
  | `review_gate` | the review gate (`review_gate.app: shared`): commit statuses, merge, update-branch | Commit statuses: write, Contents: write, Pull requests: write (reads: Pull requests: read, Contents: read) |
  | `escalations` | deployment escalation issues | Issues: write |

  ```yaml
  github:
    values:
      app:
        app_id: 123456
        private_key_file: "/etc/herdr/github-app/github-app.pem"
        installations: {berlinguyinca: auto}
        write: [review_gate]          # absent or [] = read-only App
  ```

  Tokens for a capability are repo-scoped, cached per permission set, and GitHub's answer is refused when it grants
  more than was asked (`metadata: read` may ride along). The PR poller of project intelligence and the review gate's
  reads need **Pull requests: read** on the App. A static `github.token` has no write capability.
- **The cache under the App** lists each configured owner's repositories (`repositoryOwner`, not `viewer`), and
  its catalog identity is the App's (not the hourly token), so token rotation does not purge it. Switching
  between `token` and `app` re-syncs the catalog once.

### Cross-track order (`requires`)

Components on another `follows` track (the zapp console follows `metabolomics-us/inferweave-gateway`) are staged by
their own track, so stage 1 of InferWeave does not order them by itself. A definition can name stages of other
tracks that must be rolled out first:

```yaml
requires:
  - {repo: InferWeave/inferweave, ci_context: ci/woodpecker/push/rust, stage: 1}
```

- The key is optional; a definition without it behaves as before. Branch is always `main`.
- A track's requirements are the union over its definitions. Until every named stage is rolled out on that
  track's own current target, nothing on the track is permitted. The stage decision is still made (a new release
  is still adopted), then converted like an unconfirmed release:
  - a `Start`, or a hold timer, becomes a `Hold` with the reason `waiting on <repo> <ci_context> stage <n>: <why>`;
  - a `Halt` or `Idle` still shows as it is, so a failed dependent component is still diagnosed, repaired and
    rolled back;
  - every such action carries `waiting`, and the permit gate then refuses any force, an own-stage halt included.
- "Rolled out" is read from the coordinator's own view of the other track in the same tick: its release confirmed
  (published and on main), and every component of stages up to `n` healthy on the target past its hold. A new
  release on the other track, or one of its stage-1 components failing, withdraws the permission again.
- Unknown or unmet is a hold (fail closed). The validator refuses a malformed entry, at most 8 entries, a
  requirement on the own track, on a track no definition follows or on a stage that track lacks, and any cycle
  between tracks. InferWeave's `deploy/pipelines/validate.py` does not know the key yet; it must accept it before
  a definition there uses it.

### Deployment failures: diagnosis, repairs, escalation

After each coordinator tick whose release is confirmed (published and on main), every component a `Halt`
names is diagnosed (known patterns first, then the
optional AI step of `control_plane.deployment_diagnosis`) over its redacted failure event.

- **One repair per round.** The decided repair (`retry`, `restart` or `rollback`) is recorded in a repair ledger
  per `component:release`, durably before it is used. A round covers the whole fleet of a component: failures
  herdr received before the round was decided fold into it, so the attempt cap (3) counts rounds, not nodes.
- **It reaches the host** as an additive `repair` field on the permit answer: `{id, action, attempt, reason,
  restart?, rollback_to?}`. It is present only while that failure is the component's current failed slot, usually
  next to `permitted: false`. Hosts act on each `id` once. `rollback_to` is herdr's own record of the component's
  previous good commit, never model output; a host refuses one it does not hold locally.
- **Escalation**, once per `component:release` and always internal: the record and full report on the Deployments
  page, an attention item (kind `deployment_escalation`), and one `deployment_alert` feed event with validated
  fields only. Side effects outside herdr are off by default, under `control_plane.deployment_escalation`
  (read live, fail closed):
  - `issues: true` files a GitHub issue for a software finding through the REST API with the server-held
    `github.token`, which then needs issue write access. The claim is persisted before GitHub is called, and an
    unanswered claim is never filed again.
  - `assistant: true` would notify the Personal Assistant with `dedupe_key = component:release`. The assistant has
    no notification API yet, so no sink is wired and host findings use the attention item.
  - `budget_s` (default 40, at most 45, so permits stay fresh) bounds one tick's model and GitHub calls; `issue_timeout_s` (default 15).
- No pull request is generated: a fix goes through normal CI and review.

## Inspector snapshots (faster boot)

The control plane replays every stored inspector event at boot. At ~780k events that is about nine minutes
(`boot import_state: agent, inspector and host projections` in the web log). With
`control_plane.inspector_snapshots.enabled: true` the **worker** keeps a shadow inspector fed only by that same
replay and writes it to the `inspector_snapshots` table (migration `0030`) every `interval_seconds`; the **web**
loads it and replays only newer events. When it has checked in SQL that the stored inspector rows up to the
snapshot's watermark are exactly the ones the snapshot consumed (same count), it does not even load those rows from
Postgres (they are about a third of the data); if the repo then refuses the snapshot it fetches them on demand, and a
boot never proceeds with an incomplete inspector.

It is an optimisation, never a source of truth. The web replays everything, and logs why
(`boot inspector replay: full:<reason>`), when the snapshot is missing or unreadable, was built under other
inspector limits/roles or other redaction rules, or does not match the stored events (a different event count below
its watermark, or a newer event that orders before it). The shadow never contains reasoning, which is memory-only.

Turn it on in this order: deploy with it off (the migration applies at boot), set `enabled: true` and redeploy,
wait for the worker log line `inspector snapshot written`, then redeploy once more and check
`boot inspector replay: snapshot` and the phase timing. Rollback is `enabled: false`; the table can stay.
If a redaction rule or inspector semantics change, bump `SNAPSHOT_VERSION` in `inspector.py`.

## Rakazo mobile bridge (iPhone app against the Herdr assistant)

The Rakazo mobile app can be registered as a **custom server** and then talks to the Herdr Personal Assistant: a Rakazo
"bot" is a Herdr assistant, its one chat is the assistant's thread, a message is an assistant message, a run is an
assistant run, and an approval card is an assistant approval. The bridge keeps no second assistant, task store, memory
or scheduler. Code: `control_plane/rakazo_bridge.py` (auth, procedures) and `rakazo_events.py` (wire shapes, event stream).

**Enable** (fleet YAML; the Personal Assistant must be enabled too, and the control plane restarted):

```yaml
control_plane:
  rakazo_bridge:
    enabled: true
    token_ttl_days: 30        # default
    max_tokens: 5             # live sessions per account, default
    trusted_proxies: []       # addresses/CIDRs whose X-Forwarded-For is believed (loopback always is); see below
    accounts:                 # reserved shape for a later multi-user mode; only role: operator exists today
      - email: you@example.com
        password_scrypt: "scrypt$32768$8$1$...$..."
        role: operator
        # tokens_valid_after: "2026-11-02T09:30:00Z"   # optional revocation cut-off, see "Revoking a lost phone"
```

`enabled: true` with no account, a malformed hash, an unknown key or any role but `operator` (`family` is reserved, not
implemented) stops the control plane at startup with a message naming the field. Disabled (the default) leaves every
`/rpc/*` and `/api/auth/*` path a 404, as before.

**Make the hash** without putting the password on a command line (it prompts twice on a terminal, or reads one line from stdin):

```bash
herdr-eng bridge hash-password          # prints scrypt$32768$8$1$<salt>$<hash>; paste it as password_scrypt
```

**Persist sessions** by applying migration `0036_rakazo_bridge_tokens.sql` (`herdr-eng control-plane migrate`, done by the normal
deploy). With `control_plane.pg_dsn` set the bridge stores only `sha256(token)` plus expiry, creation, last use and last
address, so a restart does not sign the phone out; without a DSN, or if the table is unreadable at boot, restarting the
control plane signs everyone out. If the table cannot be written, sign-in fails (no token is issued that would not
survive). Database calls never run under the token lock and carry a 3 s connect, statement and lock timeout whatever the
DSN says, so a stuck database costs a request seconds, not a hang.

**Revoking a lost phone.** A token that is signed out, evicted, expired or cut off is dead in memory at once; the database
delete follows, and one that fails is remembered and retried with backoff (5 s doubling to 5 min) on later token operations
(sign-out then answers an error saying so). That memory is lost on a restart, so **deleting rows and restarting is a safe
revocation only if the deletes succeeded** (check the control-plane log for `could not delete a revoked token row`). The safe
procedure does not depend on the database:

1. Set `accounts[].tokens_valid_after` to the current time (ISO-8601 with a zone, not in the future) and restart the control
   plane. Every token created before that moment is rejected, loaded or not, and its row is deleted at the next boot.
   Signing in again afterwards works (new tokens are newer than the cut-off).
2. Change the password: generate a new hash with `herdr-eng bridge hash-password`, replace `password_scrypt`, restart.
3. For a lockout of the whole account instead, remove the account from `accounts` and restart.

**Register in the app**: the sign-in screen opens a Server page (it shows the current server; **Save** probes it): enter `https://local.metabolomics.us`. The app stores only scheme://host:port and
calls `/rpc/*` and `/api/auth/*` at the origin root, so whatever fronts that name must forward those two prefixes to this
listener unchanged (the apex upstream of the fabric gateway already forwards every path to `herdr-cp-web`) and must not
buffer `text/event-stream` responses (`X-Accel-Buffering: no` is sent). Plain `http://` is accepted by the app only for
private/Tailscale addresses. Then sign in with the email and password above and create a bot in the app; an assistant
already made in the dashboard for the same account appears by itself.

**Security model.** Sign-in checks the email (case-insensitive) and a scrypt hash in constant time and always spends one
KDF, whether or not the email exists; the same `Invalid credentials` answer covers both (a body that is not a sign-in
attempt gets a generic 400 and costs nothing). Each **source address** locks out after 5 failures for 30 s, doubling up to
an hour (`429` with `Retry-After`, checked before any password work), and at most two KDFs run at once. There is
**deliberately no lockout across all addresses**: it would let any device on the tailnet keep the owner out of sign-in. The
cost of that choice is that guessing is limited per address, not globally, so use a long unique password (a scrypt hash only
slows an offline attack on the YAML, not a stolen password). A global count of recent failures is logged
(`failed sign-in from <address> (N from all addresses in 10 minutes)`) for monitoring. A token is 32 random bytes shown
once. The password, tokens, hashes and request bodies are never logged or written to the audit trail; sign-in and sign-out
are audited under the account's id, and everything the app does is audited as the account's own assistant user.

**Which address is locked out.** The lockout keys on the address the control plane attributes to the request: the TCP peer,
or the last `X-Forwarded-For` entry when the peer is loopback or inside `trusted_proxies` and the entry is an IP address.
The fabric gateway terminates TLS and forwards to the apex upstream through `rewriting_pipe(..., forwarded_for=peer)`
(`devfabric.py`, called from `fabric.py _proxy_one`), which **replaces** any client-supplied `X-Forwarded-For` with the real
client address; but it connects to the control plane from the host or Docker bridge, not from loopback. So with the default
`trusted_proxies: []` the control plane sees that connection's source (a non-loopback host/bridge address) and ignores the
header: **every client then shares one lockout key**, one device's five bad guesses lock all sign-in for 30 s and the owner
could be held out by repeating that. To key on the real client, list the address the control plane logs for a failed sign-in from the gateway
(`failed sign-in from ...` shows the key; a bridge address such as `172.18.0.1` or the host's tailnet address) in
`trusted_proxies`. I have not observed what the production gateway presents, so check the first failed sign-in after enabling
and set it. Only list proxies that overwrite the header, as the gateway does; a device that reaches the listener directly can
never choose its own key. Under `tailscale serve` the peer is loopback and the header is trusted without configuration.

Each account is its own assistant user (`rakazo:<hash of the normalised email>`), so a second account gets separate bots,
threads, memory, grants, approvals, audit identity and rate limit; the tenant still comes from
`personal_assistant.tenancy`. Approvals answered in the app go through the same `decide_approval` path as the dashboard,
with the same rules: the approver must differ from the requester for a sensitive tool unless
`personal_assistant.actions.allow_self_approval` (or `control_plane.single_operator`) already allows it. **With
`control_plane.single_operator` on, the bridge password alone is enough to approve that account's own sensitive tool
requests** (send_message, run_shell and the like); there is no second person in the loop. Use a long, unique password.

**Residual risk.** A token grants that account's full assistant access (its tools, grants and approvals) from anywhere that
can reach the listener, i.e. the tailnet; there is no second factor and no device binding. Keep the password long and
unique, keep `max_tokens` low, and see "Revoking a lost phone" if a phone is lost; nothing else is exposed (no control-plane API key route
accepts a bridge token, and no bridge route accepts an API key).

**Supported:** health probe, email/password sign-in and sign-out, `me`, `spaces/list` (one fixed space, "Herdr"),
`bots/list|get|create` (at most 20 bots per account), `threads/get|head|messages|send|stop|markRead|subscribe` (live events: new messages, run
started/waiting/completed/failed/cancelled, approval cards and their answers), `threads/answer` for approvals,
`runs/list`, `aiConsent/*` (no recipients, so no prompt), `notifications/registerPush|unregisterPush` and
`analytics/screen` (accepted and discarded). Sending runs on in the background so a slow model does not hit the app's 8 s
timeout; the reply and any failure arrive as events.

**Not supported** (answered `404 NOT_FOUND` in Rakazo's error shape so the app degrades per screen): push notifications
(nothing is delivered while the app is closed), voice and calls, billing and the model/provider screens, groups,
attachments, memory, routines, skills, connections, bot editing/archiving, and spaces other than "Herdr".
`threads/send` does not deduplicate a retried `clientNonce`.

### Board proposals on the phone (project intelligence)

Pending project-intelligence **board proposals** (`ci_rerun`, `request_ci_repair`, `propose_issue`, `link_entities`,
`pause_ingestion`) appear in the account's **first bot's thread** as an approve/reject card, so the operator can decide
them from the phone. Code: `control_plane/rakazo_proposals.py` (`ProposalDesk`); the card and run shapes are in
`rakazo_events.py`; the decision is `IntelligenceRoutes.decide_proposal`, the same function the dashboard route
(`POST /intelligence/proposals/<id>/approve|reject`) calls, so flags, approver rules, project scope, the exact
`payload_sha256` + `resource_version`, audit and the `ProposalExecutor` (a no-op unless `project_intelligence.execute`) are
shared, not copied.

**Fail closed.** Nothing is shown unless all of these hold, otherwise the app sees no card (the first reason that stops it
is logged once per account, without ids or payload text):
- `project_intelligence.enabled` and `suggest` are on and the kill switch is off;
- the bridge account has a scope in `project_intelligence.assistant_viewers["<tenant>/rakazo:<account id>"]`
  (`{org_id, projects, clearance}`), the scope its own assistant files proposals under. `default_org_id` is for the open
  listener and is never used for the phone. Out-of-org and, for a project-scoped account, other-project proposals are never
  listed and cannot be decided;
- the account may decide: `control_plane.single_operator` is on, or the account's `rakazo:<account id>` is listed in
  `project_intelligence.approvers`. Without single-operator mode the board's own rule still refuses a requester approving
  its own proposal. In single-operator mode the bridge password alone approves, including a proposal that account's own
  assistant filed (same trade-off as the tool approvals above).

**Behaviour.** A cheap read of the in-memory board runs on every `threads/get|head|messages` and at most every 5 s while a
`threads/subscribe` stream is open (no extra scheduler). One proposal is one card (keyed by proposal id). A new proposal
also posts a short bot message in the thread. The card title is built only from fields that pass the executor's validation
(for example `CI failed? Re-run owner/repo run 302`); anything that fails validation, any `propose_issue` body, and anything
token-shaped is replaced by a generic line. The detail shows the expiry and whether approving runs the action. The app
enables a card's buttons only while its run is `waiting_input`, so each pending card has a synthetic run
(`run_proposal_<id>_<hash prefix>`); the answer must quote it, and the server uses the hash and version it recorded when it
showed the card, never ones from the request. When a proposal is approved, rejected, expired, executed or failed the card
turns answered, the run ends, and a one-line outcome (the recorded reason on failure) is posted. Answering the same way twice
is a no-op ("Already approved"); the opposite answer after the fact, an expired proposal or a changed payload is refused
(409).

**Notes.** Names in `approvers` are matched against the bare principal id (`rakazo:<account id>`), not tenant-qualified. For
`propose_issue` only the title reaches the phone (never the body), so approve issue proposals on the dashboard where the
whole text is visible. A failed execution shows only the exception type and "see the dashboard", never its message. If the
account loses its standing while a card is open (kill switch, `suggest` off, scope removed) the card is marked unavailable
and its run ends. A request that wants a current view re-reads the board at most once a second.

**Limits.** There is no push: a custom server cannot reach APNs, so the in-thread message appears the next time the thread is
opened or streamed, not on the lock screen. Proposals live 30 minutes and the board and journal are both in memory, so a control-plane restart drops pending
proposals (as on the dashboard). Only the first bot's thread carries cards. Approving executes in the request; a slow CI provider can outlast the app's
8 s timeout, but execution is claimed once, so retrying is safe.
attachments, memory, routines, skills, connections, bot editing/archiving, spaces other than "Herdr", and ActionBoard
proposals (those stay on the dashboard). `threads/send` does not deduplicate a retried `clientNonce`.

## Scheduled CI watch (assistant)

`watch_ci` used to run only when the assistant model called it, so a failed CI run went unnoticed unless someone asked.
`control_plane.personal_assistant.ci_watch` adds a deterministic server-side loop (no model, no routine) that calls it
every `interval_seconds` (default 300, minimum 60; the first tick is jittered by up to 30 s) as the actor
`system:ci_watch`. Each NEW failure becomes one `request_ci_repair` and one `ci_rerun` proposal on the intelligence board,
one assistant `ci_repair` proposal and one dashboard notification; nothing is merged, pushed or restarted without an
approval, and `project_intelligence.execute` still gates execution. Off unless `enabled: true`.

Failure source. The deployed flow writes lineage `ci` nodes only from the graph merge gate's evidence writer, and those
nodes carry a commit and a check but no repository or run number, so `watch_ci` could not alert from them. The watch
therefore also reads the CI monitor's cached snapshot (`source: both`, the default): pipelines whose state is
failure/error (not killed) with a full `owner/name` repository, with a hex commit, in the user's project scope (checked before the 100-row cap) and finished within `max_age_seconds` (default 6 h; rows with no timestamp are dropped). Only the scheduled runner uses this source: the manual/tool `watch_ci` path stays lineage-only. Both sources share one fingerprint (repo, run, head), so `both` never double-alerts, dedupe and proposal path.
The repository must also be in `project_intelligence.webhook_projects` and in the user's scope, or the board refuses it
(reported as a skipped reason, not an error).

Per listed user a tick skips quietly (counted in status) when the assistant is disabled, project intelligence is
disabled or on kill switch, the user has no `assistant_viewers["<tenant>/<user>"]` entry, or no enabled assistant
exists (when `assistant_id` is omitted the oldest enabled one is used). A missing `watch_ci` grant logs one WARNING per
user per hour and does nothing; with `auto_grant: true` the loop creates the standing grant itself (scope `*`, not
preauthorized, at most `grant_max_ttl_seconds`; re-created, with an `auto_regranted` audit record, only after it EXPIRED. A revoked grant is never re-created: one hourly WARNING, skip reason `grant_revoked`) and writes an audit record
`assistant.ci_watch.auto_granted` by `system:ci_watch`. One user failing never stops another; a tick has a time budget
(`tick_budget_seconds`, default 60, also checked per row) and creates at most `max_new_per_tick` (default 10) proposals per user, the rest waiting for the next tick (`skipped_over_limit` in status); the thread stops with the server.

Status: `GET /api/v1/intelligence/status` (operator) carries `ci_watch`: `last_at`, `ticks`, `created` (running total),
`last_created`, `duplicates`, `skipped` (counts by fixed reason: `no_grant`, `no_scope`, `kill_switch`,
`intelligence_disabled`, `assistant_disabled`, `no_assistant`, `budget_exceeded`, `error`), `last_error`
(`user_failed` or `tick_failed`, never exception text) and a per-user result list.

Restarts. Dedupe is by fingerprint over the assistant store. With `personal_assistant.persistence.dsn` set the proposals
survive a restart and nothing is re-alerted. Without it the store is in memory and a restart can re-alert each current
failure once. The board proposals themselves live in memory for 30 minutes by design, so a restart (or the lapse) means
the board entry must be re-requested; the watch only does that for a failure it has not recorded.

Turning it on for the dashboard user (the open tailnet listener is the principal `open-listener`, tenant `default`):

```yaml
control_plane:
  personal_assistant:
    enabled: true
    ci_watch:
      enabled: true
      auto_grant: true          # or grant watch_ci (scope "*") once in the dashboard
      users:
        - {tenant_id: default, user_id: open-listener}
  project_intelligence:
    enabled: true
    webhook_projects: {"owner/repo": ["project-id"]}
    assistant_viewers:
      "default/open-listener": {org_id: "<org>", projects: "*"}
```

For a Rakazo bridge account add `{tenant_id: default, user_id: "rakazo:<id>"}` and an `assistant_viewers` entry
`"default/rakazo:<id>"`. The id is `rakazo:` plus the first 16 hex characters of
`sha256("rakazo-bridge-account:" + lowercase(strip(email)))`. The server logs the ids at startup
(`rakazo bridge assistant users: rakazo:...`, no email or secret), and an account must have created an assistant
(opened the app once) before the watch has an assistant to act for. If `personal_assistant.tenancy.principals` maps the
user to another tenant, use that tenant; the server logs a warning when the configured tenant disagrees.

**Approving returns at once.** `threads/answer` records the decision and audit under the routes lock and returns
`{"ok": true, "state": "approved"}` (the card shows "Allowed once", the outcome message says "Running now"); the executor
(for a CI re-run, a Woodpecker restart that can take seconds) then runs on a daemon thread owned by the desk, at most 4 at a
time with no queue behind them: a fifth approval while 4 run is refused with 429 before anything is decided. When the
execution ends the same thread refreshes the journal, so the card turns Executed or Failed and the outcome is posted
whether or not a stream is open. The executor claims the proposal itself, so a double answer, a racing dashboard approve or
a late reject still cannot run it twice or after a reject. The board is in memory: a control-plane restart mid-execution
drops the proposal with the rest of the board (nothing is left "executing"), and the action may or may not have reached
the CI provider; check the dashboard or CI.

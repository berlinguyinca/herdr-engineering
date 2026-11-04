# Hindsight shared memory on the fabric (`memory` compose profile)

Hindsight is the shared memory backend behind `herdr-memory` (unified memory spec, phase P06). It is an **optional**
part of the control-plane compose project, deployed like postgres and rustfs: configured in the central fleet file,
rendered into the transient compose env file, rolled by `deploy.sh`, routed by the existing fabric gateway.
Nothing starts unless an operator asks for it twice (`control_plane.memory.enabled: true` **and**
`deploy.sh --with-memory`).

```
other fabric hosts ──HTTPS──▶ fabric-gateway (443, existing certs) ──▶ 127.0.0.1:8888 ──▶ hindsight-api ──▶ hindsight-db
 herdr-memory client           memory.local.metabolomics.us                loopback only    (internal net)    (no host port,
 refuses plain http            memory.dev.lan                                                                  named volume)
```

* `hindsight-db`: pgvector image **pinned by digest**, only on the `hindsight-internal` network (`internal: true`), no
  published port, named volume `herdr-hindsight-db`, 4 GB / 2 CPU.
* `hindsight-api`: image `herdr-memory-hindsight:<control_plane.memory.image_tag>` built by `deploy.sh` from the
  pinned herdr-memory revision; ONNX model directory mounted read-only at `/models`; `127.0.0.1:<port>` is its only
  host binding; `no-new-privileges`, `cap_drop: ALL`, 6 GB / 4 CPU / 512 pids; no LLM (`provider: none`), no UI.
* The personal-assistant tenant (`personal`) is a separate tenant with its own key. Nothing in this stack lets a
  project key reach it, and no tenant key is placed in any harness configuration.

## Prerequisites (what the deploy host must have)

1. **An amd64 host.** The pinned digests (pgvector, hindsight slim, ONNX wheels) are verified for linux/amd64 only.
   `deploy.sh --with-memory` refuses on arm64/aarch64 with a message and changes nothing.
2. A clone of the private `berlinguyinca/herdr-memory` repo containing the pinned revision
   (`lock/upstreams.yaml` → `herdr-memory-deploy.revision`, currently `c4e0c64`) at
   `~/.local/share/herdr-engineering/herdr-memory`, or anywhere else via `--memory-checkout PATH` /
   `HERDR_MEMORY_CHECKOUT`. deploy.sh exports **exactly that revision** (`git archive`) and runs its `deploy/build.sh`;
   your checkout is never modified. `build.sh` needs `uv` and, on first build, network access to clone the pinned
   upstream Hindsight source (it verifies that revision itself).
3. The pinned E5 model directory, prepared once with the candidate's helper (it verifies every file against pinned
   hashes): `PYTHONPATH=src python3 -m herdr_memory.model_assets prepare /abs/path/e5-model`.
4. Docker with compose v2, as for the rest of the control plane.

> **bender already runs the candidate** (`herdr-memory-db-1` / `api`, `127.0.0.1:8888`, validated 2026-10-28). Two
> stacks cannot both own port 8888. Either choose another `--port` for the new one, or migrate: see *Migrating the
> candidate* below. Do not run both against the same host port.

## Setup

All on the control-plane host, as the deploying user. Nothing here prints a secret.

```sh
# 1. Create/complete control_plane.memory in the central file. Generates db_password and the four tenant keys
#    (project, global, infra, personal) with a CSPRNG, writes the 0600 file atomically after a dated backup,
#    prints only the NAMES it generated. Re-running keeps existing secrets.
herdr-eng fleet config memory init --enable \
    --endpoint https://memory.dev.lan \
    --model-dir /abs/path/e5-model          # optional: --port N, --image-tag TAG
herdr-eng fleet config memory status         # presence of each secret ("set"), never a value
herdr-eng fleet config validate              # whole file still valid

# 2. Dry run: validates config, pin, checkout, arch, `docker compose --profile memory config`; changes nothing.
deploy/control-plane/deploy.sh --dry-run --with-memory

# 3. Deploy. Control plane first (as always), then the separate memory step.
deploy/control-plane/deploy.sh --with-memory

# 4. Route it to the fabric (see "Exposure"), then run the canary checklist.
```

`--with-memory` is required on **every** deploy that should manage memory (a deploy without it never touches the
memory containers, and never recreates them). `scripts/fabric-stack.sh up` will not pass `--remove-orphans` while
memory containers exist.

### Key generation, rotation and hand-out

* Keys are 43 URL-safe characters from `secrets.token_urlsafe(32)`, generated **on the host**, stored in the 0600
  fleet file next to the Postgres/MinIO credentials, rendered into the transient 0600 compose env file (removed on
  exit) and never printed, logged or put in argv. Validation refuses duplicates (a shared key would merge tenants),
  a key equal to any Postgres/MinIO secret, and wrong shapes, naming the key but never its value.
* `herdr-eng fleet config memory init --rotate personal` regenerates one tenant's key (repeatable). Then
  `deploy.sh --with-memory` recreates the API (it reads keys at start) and the old key stops working. The database
  password is **not** rotatable this way: Postgres applies it to an empty volume only (same as the control plane's).
* A client host gets exactly the key(s) its harness may use, as a 0600 file in a 0700 directory, never in a config file
  or argv: `herdr-eng fleet config memory export-key project --out ~/.config/herdr-memory/engineering-claude.key`
  (run it on the control-plane host and carry the file over a channel you already trust, or run it there for the
  local host). Do not export `personal` to any engineering harness.
* **Known trade-off:** `control_plane.*` is in the file the control-plane web/worker containers mount read-only, so
  those processes can read the memory secrets exactly as they can read the Postgres password today. If that is not
  acceptable for the `personal` key, a follow-up should move memory secrets to their own 0700 directory (as the GitHub
  App key and `apps.key` are). Compose also exposes `POSTGRES_PASSWORD`/tenant list to `docker inspect` on the host.

## Exposure to other fabric hosts (HTTPS only)

The API never listens beyond loopback. Other hosts use `https://memory.dev.lan` (private CA, `*.dev.lan` leaf) or
`https://memory.local.metabolomics.us` (Let's Encrypt wildcard) through the fabric gateway, exactly like other
routed names:

* DNS: CoreDNS already answers `*.dev.lan` with the gateway's tailnet IP, and `*.local.metabolomics.us` is published by
  `devfabric dns publish`. No record is added.
* Certificates: both names are covered by the certificates the gateway already serves (SNI picks the certificate).
* Route: `scripts/fabric-stack.sh memory-route enable` uncomments the two marked `routes` lines in
  `deploy/fabric-stack/config.yaml` (`memory.<domain>=127.0.0.1:8888`, `memory.dev.lan=127.0.0.1:8888`); change the
  port there if you changed `control_plane.memory.port`. Then recreate the `fabric-gateway` container the way you do for
  any gateway change (`deploy/fabric-stack/README.md`, "HTTPS on 443"; it needs the variables deploy.sh renders, e.g.
  `GATEWAY_BIND`, so use the same invocation).
  Recreating the gateway drops its in-memory lease state until agents re-register (as noted in compose.yaml); do it in a
  quiet window. `memory-route disable` reverses it.
* The herdr-memory client **refuses `http://` off loopback**; do not configure it otherwise. Be aware of existing
  gateway behaviour: every named route on 443 also answers plain HTTP (the listener sniffs the first byte). Nothing
  here advertises that, the tailnet is WireGuard-encrypted, and the client will not use it, but it is a property of
  the gateway, not of this service.
* Never publish the API on a tailnet or public address, never expose the database, never use Funnel.

## Config hand-off to `herdr-eng memory install` (PR #560, branch `feat/memory-install`; not edited here)

`herdr-eng memory install` deliberately writes no credentials, and today has no way to learn the endpoint. This PR gives
it one, in this order of precedence (the first one found wins; a user's own value is never overridden):

1. `url` already present in `~/.config/herdr-memory/config.json`.
2. The fleet slice's **`memory.url`** — published by `fleet config memory init` as `sections.memory` (roles `all`,
   only a non-secret `url`), served to every enrolled host by the existing `GET /api/v1/fleet/config`, cached in
   `~/.cache/herdr-engineering/fleet-config.json`. `host_config.host_value("memory.url")` reads it live;
   `herdr_engineering.memory_hindsight_status.discover_endpoint()` reads the cache only (no network) and validates it
   (https, or http on loopback) and returns `{"url", "source", "problem"}`.

What the install branch should do once both are merged: call `discover_endpoint()`; when `url` is set, write it as
`url` into `~/.config/herdr-memory/config.json` only if that file has none, and report `problem` otherwise. Credential
files stay a separate, deliberate step (`export-key`). Its `doctor` can reuse `check_memory_backend()`.

## Backups

### Private dashboard client runtime

The backend and Memory Explorer use separate deployment steps. Configuring the
backend does not grant dashboard access. Memory Explorer still requires an
existing configured viewer and enforces its project/scope permissions; personal
and assistant banks remain excluded.

When dashboard viewers use the default canonical command, deploy.sh builds a
small CLI base from the full `dependencies.herdr-memory` lock revision. It
exports only SDK source, pyproject and README on the host. Git credentials,
models, client profiles and caches never enter the image context. A disabled
memory feature or empty viewer map needs no SDK checkout or client credentials.
An explicit custom command remains operator-managed unless a profile is supplied.

Supply an operator-owned client configuration with `--memory-client-config PATH`
or the host-only `control_plane.memory.client_config_source` field. The source
file and referenced credentials must be private user-owned regular files; symlink
and FIFO inputs are rejected. The canonical pinned `validate-config` action
checks schema and URL policy offline. Preparation does not contact Hindsight or
open the operator's cache/outbox.

Only configured engineering projects/scopes and the nonpersonal default-project
identity anchor are materialized. The existing adapter uses the `codex`
credential-binding slot; this is not agent-origin provenance. A global-only
viewer still needs the client's default project identity, but receives no new
project permission. Unused personal profiles and other harness bindings are not
exported.

At runtime, credentials go only to the private web container over Docker stdin,
into root-owned 0700/0600 files under `/run/herdr-memory-client`. The dashboard
cache is separate at `/state/herdr-memory-explorer`; its status describes that
client, not the aggregate state of all harnesses. Revoking runtime configuration
does not delete user caches, accepted outbox entries or source credentials.

Forward and previous profile artifacts are protected separately from the
fleet-config mounted directories. A failed application rollout restores the
previous image/config and previous profile; a first activation never sends its
forward profile to an older image. The optional Hindsight backend phase remains
independent. Credential rotation still requires updating client profiles after
the corresponding backend key change.

Use `deploy.sh --dry-run` first. It may create protected transient preparation
files, as existing config rendering does, but performs no Git cache update,
Docker build, runtime profile install or viewer grant. This mechanism never adds
viewer permissions. Configure a viewer explicitly through the existing private
API policy before activation; do not publish this endpoint or use Funnel.

The client URL must be reachable and trusted inside the web container. A host's
loopback address refers to the container there; use the existing authenticated
HTTPS route where appropriate. Preparation validates configuration, not endpoint
availability or authentication; runtime unavailable/degraded states remain truthful.

`herdr-hindsight-backup.timer` (installed and enabled by `deploy.sh --with-memory`, daily, `Persistent=true`) runs
`~/.local/share/herdr-engineering/memory-backup.sh backup`: `pg_dump -Fc` of the **whole** database (every tenant
schema) inside `herdr-hindsight-db` into an owner-only partial file, `pg_restore --list` verifies it, then it is renamed
to `~/.local/share/herdr-memory/backups/memory-<UTC>.dump` and its sha256 is reported (same algorithm and directory as
the candidate's `herdr-memory-backup`). It never touches the live database and prunes only older `memory-*.dump` files,
and only after a new verified dump exists (`--keep N`, default 14; `--keep 0` keeps all). Copy dumps off the host:
that is **manual** (this PR does not choose an off-host destination).

### Restore test (do this on the canary before calling the deployment accepted)

```sh
~/.local/share/herdr-engineering/memory-backup.sh backup
~/.local/share/herdr-engineering/memory-backup.sh restore-test   # newest dump -> temp DB restore_test_<ts>, count tables, drop it
```

It prints `{"restored": ..., "tables": N, "temporary_database_dropped": true}` and fails on an empty or unrestorable
dump. It never writes the live `hindsight` database. A real recovery (live database lost) is a deliberate manual
`pg_restore` into a fresh `hindsight` database after stopping `hindsight-api`; there is intentionally no automated
overwrite.

## Doctor and status

`herdr-eng doctor` adds, **only where applicable** (a host with a deploy marker, or a herdr-memory client config /
fleet `memory.url`):

| check | meaning |
|---|---|
| `memory_deploy` | marker `~/.local/state/herdr-engineering/memory/deployed.json` (image tag, port; no secrets) |
| `memory_container:herdr-hindsight` / `…-db` | docker state + health: running/healthy ok, starting warn, unhealthy/crash loop/exited/missing fail |
| `memory_health` | loopback `GET /health` |
| `memory_backup` | newest dump age: ok ≤ 36 h, warn > 36 h or none yet, fail > 7 d |
| `memory_backup_timer` | `systemctl --user is-active herdr-hindsight-backup.timer` |
| `memory_endpoint` | client side: https only (plain http off loopback = fail, never probed), then `GET <url>/health` |

`unknown` means it could not find out (no docker, daemon unreachable, host unreachable, directory unreadable); it is
never reported as ok. Also `scripts/fabric-stack.sh status` prints the two containers (or `UNKNOWN`).

## Canary checklist (one host, before any fleet use)

- [ ] `deploy.sh --dry-run --with-memory` clean; `fleet config memory status` shows every secret `set`.
- [ ] Real deploy: exit 0; control plane receipt unchanged; `docker ps` shows `herdr-hindsight` and `herdr-hindsight-db` healthy.
- [ ] `ss -ltn | grep 8888` shows **127.0.0.1 only**; nothing listens on the db port; `docker port herdr-hindsight-db` empty.
- [ ] `curl -fsS http://127.0.0.1:8888/health` ok; `herdr-eng doctor` memory checks all ok (none `unknown`).
- [ ] Missing/incorrect bearer is rejected; each tenant key works only for its own tenant (retain under `project`, confirm
      absent from `global`, `infra` and `personal`; same bank name under another tenant is excluded).
- [ ] Semantic recall of a synthetic paraphrase works (native ONNX embeddings, no network).
- [ ] From **another** fabric host after `memory-route enable`: `curl https://memory.dev.lan/health` ok (with the CA
      trusted); `http://memory.dev.lan` is not what any config uses.
- [ ] Restart `hindsight-api` and the db: data persists.
- [ ] Backup + `restore-test` pass; timer active; `doctor` shows backup age.
- [ ] Failure drills on the canary: (a) set a bad `model_dir`, run `--with-memory` → exit 4, API rolled back or (first
      deploy) stopped, control plane still healthy; (b) restore the config.
- [ ] `herdr-eng memory install --home <tmp> --dry-run` on a client host picks up the endpoint (once #560 uses the hand-off).

## Rollback and first-deploy limits

* The memory step runs **after** the control plane is deployed, verified and receipted, and drives only
  `hindsight-db`/`hindsight-api` through `--profile memory`. It never recreates, stops or removes a control-plane
  container. A failed memory step exits **4** with the control plane running as deployed.
* Health gate: both containers running, not restarting, **healthy**, restart counts stable over two samples, and the
  loopback `/health` answers. Budget: `HERDR_CP_HEALTH_TRIES` × `HERDR_CP_HEALTH_INTERVAL` (the API's own start period
  is 120 s; keep the defaults).
* **Update with a previous version running:** the previous API image id is recorded before the build; on failure it is
  re-tagged back and the API recreated from the **previous config snapshot** when one exists (else the current
  rendered config), then verified. Output: `memory rolled back to the previous image and healthy; the memory deploy
  FAILED`, or `FAILED TO ROLL BACK memory` (exit 4, control plane still unaffected). The old deploy marker is kept.
* **First memory deploy:** there is nothing to roll back to. A failed first deploy **stops** the new memory containers
  (fail safe; data volume untouched) and says so; fix the cause and re-run.
* **Not rolled back:** the database service definition (it is recreated only if its compose definition changed) and the
  volume, which is never removed by any path. Data is recovered from backups, not by rollback.
* **Turn it off:** `docker compose -p herdr-control-plane --profile memory stop hindsight-api hindsight-db` (data kept),
  `memory-route disable` + gateway recreate, `systemctl --user disable --now herdr-hindsight-backup.timer`, and set
  `control_plane.memory.enabled: false`. Removing the volume is a separate, deliberate `docker volume rm` after a
  verified off-host backup.

## Migrating the candidate on bender

The candidate (`herdr-memory-db-1`, `herdr-memory-api-1`, port 8888, its own env file) holds validated but not
production data. To move it: (1) `herdr-memory-backup` it (CLI from the candidate) and copy the dump off the container
host; (2) stop the candidate without `-v` (`docker compose -f deploy/compose.yaml down`); (3) set up and deploy this
profile (fresh volume); (4) restore the dump with `pg_restore --no-owner -U hindsight -d hindsight` into
`herdr-hindsight-db`; (5) issue **new** tenant keys with `export-key` to every client. Open item: the candidate's tenant
identities were project-specific; this profile defines `project`, `global`, `infra`, `personal`. Data stored under any
other tenant name is not visible until the tenant mapping is decided (spec question, not solved here).

## What remains manual / not verified on a real host

* Everything that needs a real host: building the image (`uv`, network), pulling digests, container health, the
  gateway recreate, certificates, DNS, the canary checklist above. The tests mock docker, `uname`, `curl`, `systemctl`
  and the herdr-memory checkout; they prove the logic and wiring, not a running Hindsight.
* Off-host backup copies and a retention policy beyond `--keep`.
* Preparing the model directory; cloning herdr-memory at the pin; bumping the pin (`lock/upstreams.yaml`).
* Deciding the tenant mapping for migrated candidate data; handing keys to client hosts; wiring `herdr-eng memory
  install` to `discover_endpoint()`.
* Moving memory secrets out of the fleet file's mounted directory (see the trade-off above).
* amd64 only. Running on arm64 needs separately verified platform digests (the deploy refuses until then).

## Operational notes from the first production deployment (bender)

* **Port.** The first deployment used `control_plane.memory.port: 8897` because 8888 was already taken by an earlier,
  unmanaged candidate. If you use a non-default port, set it in **three** places: the fleet config, the two
  `herdr-memory-route` lines in `deploy/fabric-stack/config.yaml`, and `fabric.agent_exclude_ports` (next item).
* **Keep the fabric agent off the API port.** The agent (`herdr-eng devfabric agent`) mirrors every loopback listener
  onto the tailnet IP as plain HTTP. Add the port to `fabric.agent_exclude_ports` in
  `~/.config/herdr-engineering/config.yaml` and restart `herdr-eng-fabric-agent.service`. A forwarder that already
  exists is **restored from the agent state on restart**, so also remove that port's entry from
  `~/.local/share/herdr-engineering/state/fabric-agent.json` while the agent is stopped. Verify with
  `curl http://<tailnet-ip>:<port>/health` (must fail) and `ss -ltn | grep <port>` (loopback only).
* **Routes on a running gateway.** The gateway bind-mounts `config.yaml` as a single file, so edit it **in place** (same
  inode; `sed -i` replaces the file and the container keeps the old one), then `docker restart herdr-fabric-gateway`.
  Check which checkout the running gateway uses: `docker inspect herdr-fabric-gateway` label
  `com.docker.compose.project.working_dir` (on bender it is `~/.local/share/herdr-engineering/gateway-deploy`, not
  `deploy-src`). The restart drops fabric leases until agents re-register (about a minute).
* **Clients.** `memory.local.metabolomics.us` carries a public certificate and works from any host; `memory.dev.lan`
  needs the private CA trusted. Keys are exported per tenant (`fleet config memory export-key`); never `personal` to an
  engineering harness.

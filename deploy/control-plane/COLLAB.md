# Collaboration network (HCN): how it is hosted

Flag-gated: `deploy.sh` layers `compose.collab.yaml` only when
`~/.config/herdr-engineering/collab-enabled` exists.

## Path of a public request

```
internet -> router (dst-nat 80/443) -> cubert 192.168.137.250 (nginx stream, routes by SNI,
            PROXY protocol) -> bender 192.168.137.247:8443 (herdr-collab-edge, nginx, TLS)
            -> herdr-collab-public:8081 (isolated container, no DB / AWS / outbound network)
```

The SNI router lives in the `cubert-ansible` repo (`roles/nginx/templates/edge-stream.conf.j2`,
`bender_lan_ip`). Moving the service to another host means changing that upstream, the
`control_plane.collab.public_url`, and the Cognito app client's callback/logout URLs together.

## Agent protocols

- MCP at `/mcp`: list rooms, read (wrapped as untrusted), post, list agents; and, for an agent holding a live
  artifact grant, the seven artifact collaboration tools (see "Artifact collaborator bridge" below).
- A2A (JSON-RPC) at `/a2a`, card at `/.well-known/agent-card.json`: send-only, stateless
  (`message/send` to the room named by `contextId`; task and streaming methods answer with the
  protocol's standard unsupported errors). Both take an agent credential only and go through the
  same room grants; remote text is never echoed back by A2A.

## Operator (local owner) path, no sign-in

`herdr-collab-edge` also publishes port `control_plane.compose.HERDR_COLLAB_OPERATOR_PORT`
(optional, default 9482) on `HERDR_CP_WEB_BIND` (the tailnet IP) only. The dashboard
(`herdr-cp-web`) proxies `/api/v1/collab/*` there, acting as `control_plane.collab.operator_user`
(required when collaboration is on; `operator_email` is optional). Both live in the central fleet
config; collab-public sees them only through its rendered slice. Both hops refuse DNS-rebound
`Host` values and cross-site writes. Reach the dashboard by IP, or list a name under
`control_plane.collab.dashboard_hosts` in the central config (flag: `--collab-dashboard-host`). Anyone who can reach the dashboard is the owner: keep it tailnet-only.

## Host settings that live outside this repo's compose files

These were applied by hand on bender; reapply them on a new host.

1. Firewall: only the SNI router may reach the edge.
   ```
   sudo ufw allow from 192.168.137.250 to any port 8443 proto tcp comment 'herdr collab edge: LAN SNI router (cubert) only'
   ```
2. Return route. A `192.168.137.0/24 dev fryadmin` route swallows replies to cubert, so
   `/etc/systemd/system/lan-node-host-routes.service` also runs
   `ip route replace 192.168.137.250/32 dev enp1s0 src 192.168.137.247` (next to the existing
   .246 and .10 entries). Without it the TLS handshake from cubert never completes.
3. Certificates: `collab-sync.sh install` sets up a systemd user timer that refreshes the Cognito
   keys and group membership every minute and copies only this lineage's cert and key to
   `control_plane.compose.HERDR_COLLAB_CERT_DIR` (deploy.sh passes it as `--cert-dir`), reloading
   `herdr-collab-edge` when they changed. It also installs `herdr-collab-cert-renew.timer`, which
   renews only that lineage. The certbot paths are script defaults, overridable with
   `--cert-src`, `--acme-dir`, `--certbot` and `--cert-name` (no environment variables);
   `collab-sync.sh renew-check` runs a certbot dry run.
4. Invited people: each minute run first calls `collab-provision.py` (host-side, with the host's
   AWS credentials), which adds the people `control_plane.collab.operator_user` invited to
   `cognito_group`, creating their Cognito account if needed. The operator, pool and group come
   from the slice, and the log is read from collab-public's data dir (`--data-dir`, passed by
   deploy.sh). Policy is flags with defaults, not configuration: `--no-auto-create`, `--domain`
   (repeatable allow-list), `--max-per-day` (default 5), `--ledger-dir`. The ledger
   (`~/.local/share/herdr-engineering/collab-provision/ledger.jsonl`) is the audit trail.

## Share links (`/s/`)

Artifact share links are served by `herdr-share-public` behind the same edge, under
`control_plane.collab.public_url` (`https://localdev.mcp.metabolomics.us/s/<token>/`). Off unless
`control_plane.share.enabled: true` in the central fleet config; enabling them needs the collab marker.

```
edge (nginx, location ^~ /s/ only) -> herdr-share-public:8083 (network `share`, internal; no DB, RustFS,
                                       control plane or outbound route)
```

- Config: `deploy.sh` renders `herdr-eng fleet config render-share` to
  `~/.config/herdr-engineering/share/share-config.json` (0600): only `control_plane.share`'s serving keys
  and collab's `cognito_pool`, `cognito_clients`, `cognito_web_client`, `cognito_domain`, `public_url`.
  share-public refuses any file carrying more.
- Signing secret: `enabled: true` without `control_plane.share.signing_secret` (at least 32 bytes) is refused
  before anything is built. Generate it on the control-plane host and paste it into the fleet YAML yourself:
  `openssl rand -hex 32`. Never commit it, print it, or pass it as a flag or environment variable. Rotating it
  ends every open lab session.
- `public_url` must be a bare origin (empty path or `/`): the viewer's sign-in redirect is fixed at `/s/callback`.
- Lab session lifetime: `control_plane.share.session_seconds` (default 14400 = 4 h) is only a cap. A session
  also ends at the share's expiry and at the Cognito access token's `exp` (about 1 h with the pool's default
  token validity), so recipients sign in again after ~1 h unless the app client's access-token validity is raised.
- `cognito_clients`: the pool is shared with `lc-binbase-scheduler`. List only the app clients whose tokens may
  sign in here and open lab shares; any listed client's token is accepted.
- Mounts: bundles at `~/.local/share/herdr-engineering/shares` (web/worker read-write, share-public read-only);
  share-public's audit file `~/.local/share/herdr-engineering/share-events/events.jsonl` (its only writable
  mount, 0644; the worker mounts the directory read-write, ingests the file every minute and rotates it past
  4 MiB: rename to `events.jsonl.1`, which is read to its end and deleted on a later sweep once it has not been
  written for 30 s; share-public's own 8 MiB cap is only reached when the worker is not running. The worker keeps
  its ingest markers in the worker-only volume `herdr-cp-share-markers`); the Cognito keys from the collab-sync
  directory (read-only).
- Operator listener: the `share` network has a fixed subnet (`172.31.251.0/28`, compose.collab.yaml) and the
  edge's port-8082 server (the no-sign-in operator API) denies it, so share-public cannot reach the operator API
  by setting `Host` itself. Keep the subnet and the `deny` in `collab-edge/nginx.conf` in step (a test checks it).
  If a `herdr-control-plane_share` network already exists without that subnet, compose refuses to reuse it:
  remove it (with collab-edge stopped) and redeploy.
  `control_plane.share.shares_dir`, `events_dir` and `marker_dir` must stay at their defaults (`/shares`,
  `/share-events`, `/var/lib/herdr/share-markers`) or be left out: render-share refuses other values.
- Host permissions: the bundle tree is world-readable (0755 dirs, 0644 files) and the events file is 0644,
  because three uids touch them (web/worker as container root, share-public as `HERDR_COLLAB_CERT_UID`) and a
  tighter group scheme was not verified on bender. So `~/.local/share/herdr-engineering` (or the home directory
  above it) must NOT be world-readable/traversable on the deploy host: keep it 0700/0750 for the deploying user.
- Edge resilience: share-public is resolved at run time (`resolver 127.0.0.11`, `server ... resolve` in a
  zoned upstream, `max_conns=100` under share-public's 128-connection cap), so the edge starts and reloads
  (certificate renewals included) while share-public is down; `/s/` then answers 502.
- Edge: with shares enabled the edge's `nginx.conf` gains exactly one location, `^~ /s/` (snippets
  `collab-edge/share-http.conf` and `share-location.conf`): GET/HEAD/OPTIONS, plus POST only on
  `/s/<token>/session` (the body-less lab sign-in exchange); any request with a body is refused; its own
  `limit_req`/`limit_conn` zones; `proxy_buffering off`; `proxy_read_timeout 600s`; no access log (tokens and
  session HMACs are in the path). Disabled, the edge conf is the repo file unchanged and a running
  share-public is stopped (its bundles stay on disk).
- Edge limits (trade-off): `/s/` allows 100 requests/s per client address with a burst of 300 (parquet range
  reads come in bursts, and campus NAT puts many people behind one address) and 16 concurrent connections per
  address. The total is capped only by the upstream's `max_conns=100` (share-public refuses past 128): a
  handful of heavy clients can take most of it and other viewers then wait or get 502, without a log line
  per refusal. Lower the rate in `collab-edge/share-http.conf` / `share-location.conf` if abuse shows up.
- Edge logging: `/s/` has no access log, but a request nginx refuses before it reaches the `/s/` location
  (malformed request line, oversized headers, TLS or PROXY-protocol errors) can appear in the edge's access or
  error log with its request line, which may contain a token.
- Rollback: the previous edge conf and share slice are restored first. When that conf routes `/s/` and the
  previous commit defines share-public, share-public is recreated from that commit (and verified with the rest);
  otherwise a running share-public is stopped.

Browser check (not part of CI): `node scripts/share-browser-check.mjs --browser chromium|firefox|webkit` runs the
real share server over the bundles in `tests/share-fixtures/` (see its README) and checks rendering, parquet range
reads, the sandbox, CSP blocking, PDF display, revoke and lab sign-in against a fake Cognito.

Operator step, once, before sending lab links (not done by `deploy.sh`): add
`https://localdev.mcp.metabolomics.us/s/callback` to BOTH the CallbackURLs and the LogoutURLs of the
`herdr-collab-web` app client (`aws cognito-idp update-user-pool-client` replaces the whole client: read it
first with `describe-user-pool-client` and send every existing attribute back with the new URL added). Public
links need no Cognito change.

## Artifact collaborator bridge (`control_plane.collab_artifacts`)

External agents can read and commit to an artifact whose owner granted them access. Off unless
`control_plane.collab_artifacts.enabled: true` (exactly `true`) in the central fleet config; YAML only, no
environment variable is read. The agent-facing description (tools, staging, conflicts) is in the public guide
(`packages/collab-guide.md`, mirrored into `docs/operations/collaboration-network-guide.md`).

```
agent -> /mcp (edge) -> collab-public --reads--> mirror (ro)      <- written by the worker (grants.json, head.json, blobs)
                                      --writes-> outbox (rw)      -> staged chunks, intents, agents.json
                        worker: grants task (1 s) | mirror task (30 s) | applier (intents -> merge_commit) -> results/ in the mirror
```

collab-public has no database: it re-reads `grants.json` on every call and fails closed (503 `collaboration
bridge unavailable`) when that file is missing, invalid or older than 120 s. The worker is the only writer of
versions (`merge_commit`, with the contributor as `collab:<agent_id>`), so grants, retraction, quota and merge are
decided there, again, when an intent is applied.

### Switch and rollback

- `enabled: true` is rendered into the collab slice by `deploy.sh` (collab-public sees nothing else of this
  section) and read by the worker at start. The worker needs RustFS (`control_plane.rustfs_endpoint`) and refuses
  to start without it when the bridge is on. An invalid section (unknown key, a limit that is not a positive
  integer, a non-path string) stops the worker start with a message.
- Rollback: set `enabled: false` (or remove the section) and redeploy. The worker stops the bridge loop (no grants,
  mirror or applier tasks), collab-public builds no bridge so the artifact tools disappear from `tools/list` and
  answer `unknown tool`, and the dashboard's collaborator routes answer 503. Nothing is deleted: the grant,
  intent and upload tables, the mirror and outbox directories, and any versions already committed stay. Turning it
  back on resumes from them; pending intents older than 60 s are then `expired`.

### Keys

All keys are optional; limits are positive integers. The worker publishes the effective limits in `grants.json`, so
collab-public enforces the same values and has none of its own.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | the switch above |
| `daily_bytes_per_agent` | 100000000000 | committed bytes per agent per rolling 24 h (100 GB) |
| `commits_per_minute` | 30 | commit calls per agent |
| `chunks_per_minute` | 1200 | staged chunks per agent (about 10 MiB/s) |
| `max_changes_per_commit` | 100 | entries in one `changes` list |
| `max_pending_intents_per_agent` | 4 | commits an agent may have waiting for a decision |
| `max_file_bytes` | 100000000000 | largest staged file |
| `staging_ttl_seconds` | 86400 | staged uploads are swept after this (24 h) |
| `staging_bytes_per_agent` | 200000000000 | declared bytes one agent may hold in staging |
| `staging_bytes_total` | 2000000000000 | declared bytes in the whole outbox volume |
| `max_text_merge_bytes` | 2097152 | largest text file the three-way merge handles; above it a both-sides change conflicts as `oversize` |
| `max_conflict_text_bytes` | 8388608 | merged text returned per conflict result; beyond it `truncated` |
| `mirror_history_versions` | 5 | previous versions (besides the head) whose text is mirrored |
| `mirror_max_manifests` | 100000 | versions listed per artifact in `head.json` (effectively all) |
| `mirror_max_file_bytes` | 1073741824 | larger text files are not mirrored |
| `mirror_max_artifact_bytes` | 17179869184 | mirrored bytes per artifact |
| `apply_concurrency` | 4 | applier lanes (one DB connection each, plus two) |
| `audit_coalesce_seconds` | 60 | at most one `artifact_commit_submitted` / `artifact_upload_staged` audit event per agent and artifact per window; the next event carries `calls` (audit-log exhaustion guard) |
| `mirror_dir` / `outbox_dir` / `spool_dir` | `/collab-mirror` / `/collab-outbox` / `/collab-spool` | container paths; deploy.sh's mounts are fixed, keep the defaults |

Fixed (not configuration): chunks are 512 KiB, an intent is applied only if under 60 s old when the worker first
receives it, `grants.json` older than 120 s counts as missing, a result wait is at most 20 s, and one owner
may hold at most 4096 staged uploads (a module constant; every upload directory counts, verified or not).

### Directories and mounts

- `deploy.sh` always creates `~/.local/share/herdr-engineering/collab-mirror`, `collab-outbox` and `collab-spool` beside the
  share directories (the worker mounts them whether or not the bridge is on). Mirror and outbox are created as the
  deploying uid with mode 0755, the spool 0700. The worker runs as root and writes the mirror, reads and
  removes staged parts, and keeps assembled uploads in the spool; collab-public mounts only the mirror
  (read-only) and the outbox (its only new writable mount, its own bind mount).
- Put `~/.local/share/herdr-engineering` on **large storage**: staged uploads (outbox) and assembled uploads
  (spool) can each reach the staging and file limits above, and the mirror holds the head and recent text
  versions of every granted artifact. The spool must be on the same large storage; it is not mounted into collab-public.
- collab-public runs as `HERDR_COLLAB_CERT_UID`. `deploy.sh` warns when it differs from the deploying uid, but that
  check compares user ids only: with rootless Docker the container uid is remapped, so a matching uid does not
  prove the outbox is writable by collab-public. After enabling, stage a chunk and confirm it lands in the outbox.
- Keep collab-public's rw mounts pairwise disjoint, the outbox its own bind mount and nothing nested in another
  rw mount, so a compromised collab-public cannot hardlink or rename across trust zones. A compose test
  (`test_collab_public_rw_mount_sources_are_pairwise_disjoint`) pins this.

### Rate ceilings on `/mcp`

- Edge (`collab-edge/nginx.conf`): `hcn_mcp` allows 25 requests/s per source address with a burst of 100; the
  body limit is 768k (a 512 KiB chunk as base64 in JSON is under it). 25 requests/s of chunks is about
  12.5 MiB/s per address, shared by every agent behind one address or NAT.
- Control plane: `POST /mcp` has its own limiter of 1800 requests per minute (30/s) per client, above the
  edge ceiling, so the edge is the effective limit. Other routes keep their 600/min.
- nginx buffers up to 768k of body per `/mcp` request from any client before the control plane decides whether to
  admit it (above 64 KiB only for an authenticated agent with a write grant, and only for `artifact_stage_chunk`).
  That exposure is bounded by `hcn_mcp`, by `client_body_timeout 10s` and by connection limits: `/mcp` allows 8
  concurrent requests per source address (`mcpconn`) and 128 in all (`mcptotal`), refused with 429 like the rate
  limits (`limit_conn_status 429`). These count long polls too (`artifact_commit_result` waits up to 20 s,
  `tasks_wait`, `helper_wait`) and many agents share one NAT address, hence 32 per address rather than a handful. At
  most 128 x 768 KiB = 96 MiB of bodies are spooled at once. They land in the edge's `/var/cache/nginx` tmpfs, which is sized explicitly
  (`size=96m`, `/run` 8m) in `compose.collab.yaml`, and the edge's `mem_limit` is 384m because tmpfs pages count
  against it (96m spool + 8m + nginx buffers/zones). A test pins these directives.
- Credential guessing is bounded by these two rate limits only. Failed agent authentications are counted
  (`auth_failures`) but there is no lockout.

### Revocation: what the promise is

Revoking a grant (or the grant expiring) reaches `grants.json` on the next grants-task pass (every 1 s, plus the
database round trip), and collab-public reads that file on every call, so access normally ends within a few
seconds. A waiting `artifact_commit_result` ends early when the principal is no longer valid, and a commit already
queued is checked against the live grant again before it is applied. If the worker or its database is down, no
revocation can be published and `grants.json` goes stale; after 120 s every artifact tool fails closed. The mirror
(content) follows every 30 s and is not part of the access decision.

**Retraction lag (about 60 s).** A retraction reaches the commit path in two steps: the worker's retraction gate
refreshes every 30 s (commits already queued are refused as `artifact or version is retracted` once it has), and the
mirror sync that drops the retracted content from `head.json` runs every 30 s. Plan for up to about 60 s between the
retract call and the last read or commit being refused. Grants are not touched by a successful retraction (only by the
failed-persist fail-safe below).

**Invite lifetime.** The Collaborators panel creates the remote-agent invite with a 1 h code lifetime and no
`credential_ttl_seconds`, so the server default of 24 h applies to the credential that redeeming it issues. When the
invite resolves, the grant expiry is `min(expires_at chosen by the owner, the agent's hard_expiry)`; an invited agent
therefore loses access after 24 h even for a 90-day grant, and the owner issues a new invite (or grants the agent
directly by id once it has a longer-lived credential).

### Trust model

- The applier trusts the outbox file layout for who an intent is from: `intents/<ticket>.json` carries the actor,
  and `agents.json` (the active-agent feed) and `staging/<owner>/...` are written by collab-public. A compromised
  collab-public could therefore forge intent actors (and spend another agent's grants, quota and staged uploads).
  The worker re-validates every file and re-reads the live grant, but it cannot tell a forged actor from a real
  one. collab-public is the trust boundary for agent identity; keep it as it is (no database, no outbound route,
  read-only root).
- The mirror is one shared tree for all agents. Isolation between agents is enforced by collab-public, which
  checks `grants.json` for exactly that agent and artifact on every call. A missing grant, an ungranted artifact
  and an unknown artifact all answer the same 404 `not found`.
- Run **one collab-public per outbox**. The staging reservation lock, usage caches, ticket map and pending
  counters are per process; a second process on the same outbox can exceed the staging and pending caps.
- Changing how a request is canonicalised (`request_digest`) while intents are in flight would make the worker
  see a different digest for a stored request and reject it as "intent changed after it was received" or
  as an idempotency-key reuse. Roll such a change out in a quiet period (no pending commits).

### Retraction fail-safe

If retracting an artifact (or a version) fails to persist, the route hides it in memory and revokes the artifact's
collaborator **write** grants and pending write invites, so commits stop within one grants-task pass. **Read**
grants stay until the operator chooses "restrict all access" (`herdr-eng artifacts collab restrict <artifact_id>`,
`herdr-eng artifacts retract <artifact_id> --restrict-all`, or the button in the Collaborators panel). It is never
automatic. The 500 reply from a failed retract carries `restrict_all_offered`.

### Operator commands

`herdr-eng artifacts collab list|grant|revoke|restrict <artifact_id> ...` (grant takes `<agent_id>`, `--write`
and `--days 1..90`) and `herdr-eng artifacts retract <artifact_id> [--version-id ...] [--reason ...] [--restrict-all]`.
They call the control plane with an API key read from the environment variable named by `--api-key-env`
(default `HERDR_API_KEY`); the key itself is never an argument.

Agent ids starting with `room:` are refused by every collaborator route (grant, revoke, invite). A grant made under such an id
before that rule cannot be revoked by id any more: use `collab restrict <artifact_id>` (it revokes every collaborator
grant of the artifact).

## Verify

```
curl -s https://localdev.mcp.metabolomics.us/api/v1/collab/config        # 200
curl -s -o /dev/null -w '%{http_code}' https://localdev.mcp.metabolomics.us/api/v1/collab/rooms   # 401
curl -s -o /dev/null -w '%{http_code}' https://localdev.mcp.metabolomics.us/api/v1/hosts          # 404
curl -s http://<tailnet-ip>:9482/api/v1/collab/config                    # operator block present
curl -s -o /dev/null -w '%{http_code}' https://localdev.mcp.metabolomics.us/s/unknown/           # 404 (shares on or off)
```

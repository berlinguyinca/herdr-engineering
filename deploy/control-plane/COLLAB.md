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
`control_plane.share.public_url`, a host of its own (`https://<share host>/s/<token>/`); MCP stays on
`control_plane.collab.public_url`. Off unless
`control_plane.share.enabled: true` in the central fleet config; enabling them needs the collab marker.

```
edge (nginx, the share host's own server: /s/ only) -> herdr-share-public:8083 (network `share`, internal; no DB, RustFS,
                                       control plane or outbound route)
```

- Config: `deploy.sh` renders `herdr-eng fleet config render-share` to
  `~/.config/herdr-engineering/share/share-config.json` (0600): only `control_plane.share`'s serving keys
  and collab's `cognito_pool`, `cognito_clients`, `cognito_web_client`, `cognito_domain`, `public_url`
  (the collab URL is carried but share links no longer use it).
  share-public refuses any file carrying more.
- Signing secret: `enabled: true` without `control_plane.share.signing_secret` (at least 32 bytes) is refused
  before anything is built. Generate it on the control-plane host and paste it into the fleet YAML yourself:
  `openssl rand -hex 32`. Never commit it, print it, or pass it as a flag or environment variable. Rotating it
  ends every open lab session.
- `control_plane.share.public_url` must be a bare lowercase https origin (empty path or `/`; no userinfo, query,
  fragment, non-ASCII or trailing-dot host) on a host other than `control_plane.collab.public_url`'s: the viewer's
  sign-in redirect is fixed at `/s/callback` at its root. `deploy.sh` refuses anything else.
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
- Edge: with shares enabled the edge's `nginx.conf` gains the zones, upstream and a `server` for the share host
  (snippet `collab-edge/share-http.conf`; `@share_host@` is the host of `control_plane.share.public_url`, rendered by
  `herdr-eng fleet config render-edge`). The MCP host serves no `/s/` at all (404). The share server proxies only
  `/s/`; everything else is a 404. `location /s/`: GET/HEAD/OPTIONS, plus POST only on `/s/<token>/session` (the
  body-less lab sign-in exchange); any request with a body is refused. The annotation endpoints
  (`/s/<token>/k/<session>/annotations[/ann_<id>/edit|delete]`) are a separate location: GET and POST only, a 32k
  body cap, their own `share_annotate` zone (5 r/s, burst 30). Both have `limit_conn share_conn 16`,
  `proxy_buffering off`, and no access log (tokens and session HMACs are in the path). The edge adds no caching and
  no response headers beyond HSTS, so share-public's CSP and `Cache-Control: no-store` reach the browser unchanged;
  `Origin`, `Sec-Fetch-Dest`, `Range` and `Content-Length` are passed on, `X-Forwarded-Host` is dropped. Disabled, the
  edge conf is the repo file unchanged and a running share-public is stopped (its bundles stay on disk).
- Certificate: the edge presents ONE certificate for both hosts (`/etc/acme`); `collab-sync.sh` is unchanged and
  still manages one lineage. Before enabling shares, add the share host to the existing lineage once, on the
  certbot host, with the same DNS-01 plugin and flags the lineage was created with:
  `certbot certonly --cert-name localdev.mcp.metabolomics.us --expand -d localdev.mcp.metabolomics.us -d mcp.local.metabolomics.us -d artifacts.local.metabolomics.us -d <share host>`
  (every name the lineage already has must be repeated). Renewals then cover both names. The router and SNI front
  must also send the share host's name to the edge's port.
- Edge limits (trade-off): `/s/` allows 100 requests/s per client address with a burst of 300 (parquet range
  reads come in bursts, and campus NAT puts many people behind one address) and 16 concurrent connections per
  address. The total is capped only by the upstream's `max_conns=100` (share-public refuses past 128): a
  handful of heavy clients can take most of it and other viewers then wait or get 502, without a log line
  per refusal. Lower the rate in `collab-edge/share-http.conf` if abuse shows up.
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
`<control_plane.share.public_url>/s/callback` to BOTH the CallbackURLs and the LogoutURLs of the
`herdr-collab-web` app client (`aws cognito-idp update-user-pool-client` replaces the whole client: read it
first with `describe-user-pool-client` and send every existing attribute back with the new URL added). Public
links need no Cognito change. Keep the previous host's `/s/callback` registered until the deploy is verified: a
rollback to the previous release signs in there. Existing links on the old host stop working at the cutover
(re-issue them); see docs/operations/collaboration-network.md.

## Member artifacts (`artifacts.metabolomics.us`)

Members (the Cognito group `control_plane.collab.members.group`) own artifacts, upload and publish them in the
browser, and can create public or lab share links for them. One host, `control_plane.share.public_host`, serves it all:
`/s/` (share-public, as above), `/api/member/` and the member page `/` and `/my/` (collab-public). Off unless
`share.public_host` is set; the member locations also need `control_plane.collab.members.enabled: true`.

```
edge (nginx, server_name <public_host>): /s/ -> share-public:8083
                                         /api/member/, /, /my/ -> collab-public:8081 (own zones member_req / member_conn)
```

- Edge: `collab-edge/member-http.conf` is spliced into the host's server only when members are enabled, and its two
  zones (`member_req` 10 r/s, `member_conn`) into the http block. `PUT /api/member/artifacts/<id>/uploads/<id>/runs/<n>`
  alone gets `client_max_body_size 17m` and `proxy_request_buffering off` (32 chunks x 512 KiB + slack); everything
  else under `/api/member/` is 1 MiB. No access log on `/s/` and `/api/member/` (tokens and the session exchange).
  `X-Frame-Options: DENY` and HSTS are set by the edge (the app's copy is hidden so it is sent once); the page's CSP is
  the app's. The MCP host serves no `/s/` and the new host serves no `/mcp`.
- Operator steps, **not done by `deploy.sh`**:
  1. DNS: `artifacts.metabolomics.us` to the public edge.
  2. Add the hostname to cubert's SNI stream edge (`networking/cubert-ansible`, stream map to the collab edge backend)
     and run its playbook.
  3. Certificate: the collab edge's ACME lineage must name the new host (`certbot certonly --cert-name ... --expand
     -d <existing names> -d artifacts.metabolomics.us`); `deploy.sh` refuses a certificate that does not.
  4. Cognito: add `https://artifacts.metabolomics.us/s/callback` AND `https://artifacts.metabolomics.us/my/callback` to
     both CallbackURLs and LogoutURLs of the `herdr-collab-web` app client. `update-user-pool-client` replaces the
     whole client, so read it first with `describe-user-pool-client` and send every existing attribute back with the
     new URLs added. Careful: that output may contain `ClientSecret` (never paste or log it; redirect the JSON to a
     0600 file) and read-only fields (`ClientId`, `UserPoolId`, `CreationDate`, `LastModifiedDate`, `ClientSecret`)
     that `update-user-pool-client` rejects: strip them (for example `jq 'del(.UserPoolClient.CreationDate,
     .UserPoolClient.LastModifiedDate, .UserPoolClient.ClientSecret)'`) and pass `ClientId`/`UserPoolId` as options.
     Changing `public_host` later means registering both callbacks on the new host first; keep the old host's until
     verified.
  5. Fleet YAML: set `control_plane.share.public_host`, `control_plane.collab.members.enabled: true`,
     `control_plane.collab.members.group`, `control_plane.collab.members.session_secret` (`openssl rand -hex 32`,
     pasted in; never committed, printed or passed as a flag) and, if collab-public does not run as the default uid,
     `control_plane.collab.members.secrets_gid` (the gid collab-public runs as). The worker gets
     `HERDR_COLLAB_CERT_UID` (default 1000, as collab-public) from compose; if neither is available the worker REFUSES
     to start members (fail closed) with a message naming `control_plane.collab.members.secrets_gid`.
  6. Verify: `curl -s https://artifacts.metabolomics.us/api/member/config` returns 200, then a sign-in round-trip at
     `/`. Run `WORKER_CONTAINER=<worker> deploy/control-plane/member-secret-smoke.sh /tmp <collab-public-uid>` as root on the worker
     host once (it checks the worker's own gid against the uid, refuses uid 0/65534; PYTHON/PYTHONPATH must give a
     checkout both users can import).
  7. Rollback: remove `public_host` / `members.enabled` and redeploy (the edge returns to the share-only server or
     the previous conf; rows, blobs and bundles stay). Members' public links stop with the host.
- Group-readability caveat: any process whose gid is the secrets gid can read a pending share URL for up to 60 s
  (until collab-public takes it or the worker's sweep deletes it) and can delete it (denial only). The default gid is
  collab-public's uid, usually the deploying operator's own login group; prefer a dedicated group (set
  `control_plane.collab.members.secrets_gid` and run collab-public as it) over the operator's login group.
- Secret hand-off between uids (important): the worker (root) writes the read-once share URL to
  `<outbox>/secrets/<ticket>.json` and collab-public (uid `HERDR_COLLAB_CERT_UID`) reads and deletes it. The worker
  therefore sets that directory to group `control_plane.collab.members.secrets_gid` (default: the worker's
  `HERDR_COLLAB_CERT_UID`, which compose passes with collab-public's default), mode 0770, files 0640; others get
  nothing. With no gid the worker refuses to start members rather than issue links nobody can receive.
- Approving a share link is a human action: `member.share_approve` accepts user actors only and the agent tool
  `artifact_share_approve` no longer exists; an agent can only create a pending share that the owner approves in the web page.
- A `share` grant is not read-protected: a grantee holding only `share` can create and approve a link, receives the
  read-once URL, and so can open it and read the bytes. The member API (head, history, view tools) stays gated on
  `read`; do not hand out `share` expecting it to hide content.
- Public links by members (D3): members can create public links to their artifacts. The operator sees them in the
  Members panel and with `herdr-eng artifacts members links --audience public`; revoke there.
- Limits and behaviours to know:
  - The provenance banner on member links is best-effort: the page's own CSS/JS can hide it; the containment is the
    sandbox CSP, opaque origin, group/expiry and revoke. Public links get the bar on HTML only.
  - Member HTML is capped at 2 MiB; svg, pdf, xhtml and xml are refused for member links, and HTML whose markup
    cannot be proven to render the bar (for example scripts containing `a <!b`, or content before `<body>` beyond
    head elements) is refused rather than labelled.
  - The operator-API approve path shows the user id, not the display name; the member web path shows the name.
  - Removing a member from the group takes effect after the claim TTL (`members.groups_ttl`, 24 h) and then a grace
    (`members.removed_grace`, default 3600 s of continuous removed observation) before their shares are revoked.
    Group REMOVAL is only observed at a sign-in: a member who never signs in again keeps the last seen groups. A
    DISABLED account is different: it reports `disabled` at any claim age (the TTL only ages group membership), and
    the sweep revokes their links.
  - Effective upload limits are the smaller of the member setting and the collab_artifacts bridge limit
    (`max_upload_bytes` vs `max_file_bytes`; `staging_bytes_per_member` vs the bridge staging caps). `null` for
    `max_upload_bytes` or `staging_bytes_per_member` means "only the 1 TiB structural bound".
  - `members.enabled` needs `share.enabled`, `collab_artifacts.enabled: true` and at least one `api_keys` entry;
    `herdr-eng fleet-config check` names the missing key. With no API keys the open listener makes every caller an
    operator, so the member routes would be reachable by anyone on that listener.
  - Orphan blobs left by conflicting publishes stay counted against the member's quota until an operator purge.
  - Purge refuses held tombstones and tombstones without a purge date; it anonymises annotations, share events and
    feedback of third parties, and blanks file names in `output_manifest`, `provenance.message`/`entry` and
    `artifact_shares.entry` (migrations 0048, 0049 and 0050; renumber at merge if another branch took the number).
    Storage keys and hashes are kept. The append-only jsonl event files on disk are written by a separate process and
    are NOT rewritten by purge; they keep the old names until the retention window of that log removes them.
  - Purge also blanks the message and conflict file names of collaborator merge attempts. Lifecycle event reasons are
    server-fixed strings (`member delete`, `member purge`), so nothing member-written remains there.
  - A shared blob survives the deletion of one owner while another artifact (including a held or restorable
    tombstone) still references it.
  - `share_creates_per_hour` (default 20) is counted per process in memory.
- Verifying the rendered edge: `docker run --rm --add-host collab-public:127.0.0.1 --add-host share-public:127.0.0.1
  -v <conf>:/etc/nginx/nginx.conf:ro -v <certdir>:/etc/acme:ro nginx:1.29-alpine nginx -t` (a test does this when
  Docker is available).

## Artifact host (`control_plane.artifact_host`)

The dedicated artifact host (`herdr_artifact_host`, image `herdr-artifact-host`) replaces share-public for member
artifacts. It is enabled with `control_plane.artifact_host.enabled: true` (needs the collab marker; refused together
with `control_plane.share.enabled`, since both want the edge's `/s/`). `deploy.sh` renders the slice with
`herdr-eng fleet config render-artifact-host`, builds the image, runs `migrate`, brings the host up with `--wait`
BEFORE the control plane and the edge, and stops it again when the section is disabled. `--dry-run` renders and
validates the slice and the compose files without printing any secret.

- Services (`deploy/artifact-host/compose.yaml`): `artifact-host-db`, `artifact-host-migrate` (one-shot),
  `artifact-host-public` (8090), `artifact-host-internal` (8091) and `artifact-host-relay` (a pinned, hardened nginx).
  No host ports are published. Each role has its own compose healthcheck (`herdr-artifact-host healthcheck <role>`,
  loopback port 8092 on public).
- Database roles (three, none a superuser; `artifact_admin` is only the bootstrap account and is used by `deploy.sh`
  for roles, backup and restore): `artifact_owner` runs migrations (DDL), `artifact_public` has DML only,
  `artifact_internal` has SELECT only and `default_transaction_read_only`. Each service gets only its own slice
  (`public.json`, `internal.json`, `migrate.json`) and its own password, derived by HMAC from `db_password`.
  `roles.sql` is idempotent and is applied through the local socket on every deploy. Rotating `db_password` and
  redeploying changes all role passwords (the roles step runs before the services restart). `db_user` is not a setting.
- Networks: every host network is `internal: true` with `gateway_mode_ipv4: isolated`, so a container has neither a
  route out nor a host-side gateway to dial, and external names do not resolve.
  - The public role needs no egress. Cognito keys and group membership arrive through the synced `jwks.json` and
    `members.json` (mounted read-only from `HERDR_COLLAB_SYNC_DIR`), and the browser exchanges the sign-in code with
    Cognito itself.
  - The edge reaches public over `<prefix>-edge` (fixed subnet 172.31.252.0/28, also denied on the `:8082` operator
    listener). `control-plane-web` is NOT on a host network that reaches the internal role: it joins only
    `<prefix>-relay`, where the relay (GET-only `/v1/`, everything else 405/404) forwards to the internal role on
    `<prefix>-internal` (172.31.252.16/28, internal role at the fixed address .18). Set
    `control_plane.artifact_library.host_url: http://artifact-host-relay:8091`.
  - The database sits on its own network with the public, internal and migrate roles; db and internal cannot
    reach each other except through their own roles, and public and internal share no network.
- Fixed names and addresses: the compose project (`herdr-artifact-host`), network prefix, both subnets and the
  internal address are not deploy settings. `deploy.sh` exits 2 if `HERDR_ARTIFACT_HOST_PROJECT`,
  `HERDR_ARTIFACT_NET_PREFIX`, `HERDR_ARTIFACT_EDGE_SUBNET`, `HERDR_ARTIFACT_INTERNAL_SUBNET`,
  `HERDR_ARTIFACT_INTERNAL_IP`, `HERDR_ARTIFACT_HOST_DIR` or `HERDR_ARTIFACT_HOST_TAG` is set in its environment.
  `public_host` must not equal an `mcp_public_hosts` name or the dashboard's name, and the secrets need real entropy.
- Config secret: Compose file secrets are bind mounts and ignore `uid`/`gid`/`mode`. `deploy.sh` therefore installs
  the slices and the db admin password as 0644 files inside a 0700 directory
  (`~/.config/herdr-engineering/artifact-host/`); `roles.sql` stays 0600. The directory mode is the access control.
- Deploy order and safety: certificate check (the edge certificate must name `public_host`, as for share), image
  build, database up (never force-recreated on a routine deploy), roles, `pg_dump` backup to
  `artifact-host/backups/pre-<tag>-<time>.dump` (0600; a failed backup stops the deploy before migrating), migrate,
  public/internal/relay recreated with `--wait`, then the control plane and edge, then a probe of
  `https://<public_host>/s/probe/` through the edge.
- Rollback: the deploy records the previous state (`artifact-host.state`: `enabled` or `disabled`). A failed deploy
  rolls the control plane back with the overlay only if the previous release ran the host. If the host was off before,
  it is stopped again and the files this deploy installed are removed. If it was on and this deploy had not yet
  touched the database, the previous host release is brought back from the rollback worktree. If roles or migrations
  already ran, the host is not silently reverted (the previous code may not run against the changed schema): the
  deploy exits 3 and prints the exact manual steps (stop the roles, `pg_restore` of the backup, redeploy the previous
  ref).
- Python: `deploy.sh` runs the fleet commands with the repo's `.venv/bin/python` (override with
  `HERDR_DEPLOY_PYTHON`), falling back to `python3`.
- Membership: the host reads `members.json` and `jwks.json` from the synced directory (`SyncedDirectory`).
  `collab-sync.sh` writes the optional `disabled` list into `members.json`; the host treats a stale or empty listing as
  no evidence (sign-in fails closed) and denies accounts on the `disabled` list.
- Edge (`artifact-host-http.conf`, `artifact-host-mcp.conf`, rendered in place of the share snippets): the artifact
  host name routes `/s/`, `/api/member/`, `/` and `/my/` to `artifact-host-public:8090` (run-time DNS via the
  `resolver`); `mcp.` routes `POST /mcp/artifacts` there too. Nothing routes to the internal role. Each route class
  has its own upstream (`share`, `member`, `upload`, `mcp`) with its own `max_conns`, so a flood on one cannot
  starve the others (limits are in `artifact-host-http.conf`). `X-Forwarded-For`
  is overwritten with `$remote_addr`; on `/mcp/artifacts` `Forwarded`, `X-Real-IP` and `X-Forwarded-Host` are also
  stripped; `/healthz` and `/internal/` answer 404 at the edge. Body caps: `/s/` 1k,
  annotations 32k, `/api/member/` 1m, run uploads 17m, `/mcp/artifacts` 768k. Existing room routes are unchanged.
- Check: `tests/test_artifact_host_compose.py` parses the files and, when Docker is available (required under CI or
  `HERDR_REQUIRE_DOCKER=1`), starts the stack and runs `nginx -t` on the rendered edge.

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

## Fabric listener (optional)

Set `control_plane.collab.fabric_trust.listen_host` (a tailnet address) and `listen_port` (and `whois_socket: /run/fabric-whois/whois.sock`)
and `deploy.sh` layers `compose.collab-fabric.yaml`: a `fabric-whois` sidecar (no network, tailscaled's socket directory read-only, its own
socket directory) and a port published from `collab-public` on that tailnet address only. Without those keys nothing changes. Before relying
on it, check that Docker preserves the caller's source address, and read the rollback caveat: `docs/operations/fabric-trust.md` section 9.

Three host steps `deploy.sh` cannot do for you (it warns while they are missing):

- **Firewall (required).** The `fabric` network is not internal (Docker cannot publish a port from an internal network) and masquerade-off does not remove
  its default route, so `collab-public` could reach the host's own addresses, other containers' ports and the LAN. As root:
  `deploy/control-plane/collab-fabric-firewall.sh --install` (`--check`, `--remove`); it drops NEW connections from `172.30.213.0/24` in `DOCKER-USER`
  and `INPUT`. Re-apply at boot.
- **Boot order.** Install `deploy/control-plane/docker-after-tailscale.conf` as `/etc/systemd/system/docker.service.d/after-tailscale.conf`
  (`systemctl daemon-reload`): docker then starts after tailscaled and waits for the tailnet address, otherwise the port bind can fail at boot and the
  PUBLIC site stays down.
- **The collab host trusts itself.** A local process, `ssh -L`/`socat` relay or host-network container reaches the listener as the host's own tailnet IP and is
  accepted as that machine if the host is in `machines`. Run no relays or L4 fronts on the collab host and omit it from `machines` unless its own agents need it.

## Verify

The MCP URL is `https://mcp.local.metabolomics.us`. `localdev.mcp.metabolomics.us` is a deprecated alias the edge serves on the same server block during the overlap (the certbot lineage keeps that name).

```
curl -s https://mcp.local.metabolomics.us/api/v1/collab/config        # 200
curl -s -o /dev/null -w '%{http_code}' https://mcp.local.metabolomics.us/api/v1/collab/rooms   # 401
curl -s -o /dev/null -w '%{http_code}' https://mcp.local.metabolomics.us/api/v1/hosts          # 404
curl -s http://<tailnet-ip>:9482/api/v1/collab/config                    # operator block present
curl -s -o /dev/null -w '%{http_code}' https://mcp.local.metabolomics.us/s/unknown/           # 404 (the MCP host serves no /s/)
```

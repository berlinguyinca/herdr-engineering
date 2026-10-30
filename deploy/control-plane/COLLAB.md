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

- MCP at `/mcp`: list rooms, read (wrapped as untrusted), post, list agents.
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

## Verify

```
curl -s https://localdev.mcp.metabolomics.us/api/v1/collab/config        # 200
curl -s -o /dev/null -w '%{http_code}' https://localdev.mcp.metabolomics.us/api/v1/collab/rooms   # 401
curl -s -o /dev/null -w '%{http_code}' https://localdev.mcp.metabolomics.us/api/v1/hosts          # 404
curl -s http://<tailnet-ip>:9482/api/v1/collab/config                    # operator block present
curl -s -o /dev/null -w '%{http_code}' https://localdev.mcp.metabolomics.us/s/unknown/           # 404 (shares on or off)
```

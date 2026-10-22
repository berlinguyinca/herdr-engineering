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

## Verify

```
curl -s https://localdev.mcp.metabolomics.us/api/v1/collab/config        # 200
curl -s -o /dev/null -w '%{http_code}' https://localdev.mcp.metabolomics.us/api/v1/collab/rooms   # 401
curl -s -o /dev/null -w '%{http_code}' https://localdev.mcp.metabolomics.us/api/v1/hosts          # 404
curl -s http://<tailnet-ip>:9482/api/v1/collab/config                    # operator block present
```

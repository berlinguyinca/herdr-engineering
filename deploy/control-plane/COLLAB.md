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
`--collab-public-url`, and the Cognito app client's callback/logout URLs together.

## Operator (local owner) path, no sign-in

`herdr-collab-edge` also publishes port `HERDR_COLLAB_OPERATOR_PORT` (default 9482) on
`HERDR_CP_WEB_BIND` (the tailnet IP) only. The dashboard (`herdr-cp-web`) proxies
`/api/v1/collab/*` there, acting as `HERDR_COLLAB_OPERATOR_USER`. Both hops refuse DNS-rebound
`Host` values and cross-site writes. Reach the dashboard by IP, or add a name with
`--collab-dashboard-host`. Anyone who can reach the dashboard is the owner: keep it tailnet-only.

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
   `HERDR_COLLAB_CERT_DIR`. The certbot deploy hook also reloads `herdr-collab-edge`.

## Verify

```
curl -s https://localdev.mcp.metabolomics.us/api/v1/collab/config        # 200
curl -s -o /dev/null -w '%{http_code}' https://localdev.mcp.metabolomics.us/api/v1/collab/rooms   # 401
curl -s -o /dev/null -w '%{http_code}' https://localdev.mcp.metabolomics.us/api/v1/hosts          # 404
curl -s http://<tailnet-ip>:9482/api/v1/collab/config                    # operator block present
```

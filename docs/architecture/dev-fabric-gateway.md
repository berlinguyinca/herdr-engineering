# Dev Fabric Gateway (spec 0110, component #3)

The fabric gateway is the fleet's **single front door** for the `dev:<port>`
address space: from any Tailscale device, `http://dev:<port>` resolves to one
stable service name regardless of which machine the application runs on.

## Why a gateway

A per-host forwarder gives each machine its own ports, but the *unified* view
required by spec 0110 needs:

1. **One stable name** — the fleet's `dev` logical service (a Tailscale
   MagicDNS name, e.g. `bender.tail0c50da.ts.net`), independent of where the
   app runs.
2. **Cross-host routing** — a lease registered by `beast` must be reachable
   through the gateway's address, not beast's.
3. **One registry, one allocator** — collision-free external ports across the
   whole fleet, with heartbeat/expiry/stale cleanup in one place.

The gateway therefore owns the registry, the router, and the health probes.
Hosts only run registrar commands (`herdr-eng devfabric register|heartbeat|
close|list`).

## Components

```
                 ┌────────────────────────── gateway host (bender) ──────────────────────────┐
                 │                                                                           │
  tailnet ─────► │  control API  :29999 (tailnet+loopback, bearer token)                     │
  (any host)     │      │        register / heartbeat / close / list / resolve / healthz     │
                 │      ▼                                                                     │
                 │  FabricGateway ── probe loop (every probe_interval):                       │
                 │      │                TCP-probe each active lease target;                 │
                 │      │                healthy → renew, unhealthy → let TTL expire;        │
                 │      │                reload store if an overlapping process wrote it;    │
                 │      │                sync router to active leases                        │
                 │      ▼                                                                     │
                 │  shared registry (state/leases.json)  +  gateway.lock (pid:nonce)         │
                 │      │                                                                     │
                 │      ▼                                                                     │
                 │  GatewayRouter ── for each active lease: bind external port on            │
                 │                   loopback + tailnet, byte-proxy to target_host:port      │
                 └───────────────────────────────────────────────────────────────────────────┘
  client (any tailnet device, incl. phone/iPad)
      curl http://bender.tail0c50da.ts.net:18000   ──►  router :18000  ──►  beast:5678 (app)
```

- **Control API** — `FabricGateway` exposes a small HTTP control plane
  (`POST /register`, `POST /heartbeat`, `POST /close`, `GET /list`,
  `GET /resolve/<port>`, `GET /healthz`). It binds to the tailnet IP and/or
  loopback only (private by default) and requires a bearer token when a token
  is configured (`HERDR_ENGINEERING_FABRIC_TOKEN` or
  `~/.config/herdr-engineering/fabric-token`; never in the repo).
- **GatewayRouter** — dynamic per-lease listeners. Each leased external port
  is bound on every configured interface (loopback + tailnet) and proxied to
  the lease's `target_host:target_port` with `proxy_pair()` (shared with the
  per-host `DevServiceForwarder`). The proxy is protocol-transparent
  (HTTP/HTTPS/WS/SSE/HMR/generic TCP). Unbinding joins the accept-loop
  threads, so a port is only reported released when it truly is.
- **Probe loop** — TCP-probes each active lease target every
  `probe_interval_seconds` (default 15). A healthy probe renews the lease's
  heartbeat; an unhealthy one does not, so `lease_ttl_seconds` (default 90)
  expiry + reconcile removes the route and frees the port. The loop also
  re-reads the store if an overlapping process changed it and re-syncs the
  router to the active set (self-healing after restarts or close calls).
- **Single-writer lock** — `gateway.lock` (pid:nonce) beside the store
  prevents two gateways from owning one registry. Stale locks from dead PIDs
  are taken over automatically; same-PID re-entrancy (tests) is rejected via
  the nonce.

## Addresses

| Form | Meaning |
|---|---|
| `http://<magicdns>:<ext>` | The `dev` front door — stable for the fleet, any tailnet device |
| `http://<tailnet-ip>:<ext>` | Same service via raw IPv4 (no DNS needed) |
| `http://127.0.0.1:<ext>` | Gateway host's loopback view of the same router |

`url_base` in config (or the gateway's own MagicDNS name) is advertised in
every `register`/`list`/`resolve` response as the lease's `url`.

## Registration flow

1. App starts on machine X (bound to the tailnet interface or `0.0.0.0`).
2. `herdr-eng devfabric register --machine X --host auto --port <app>` on X:
   - `--host auto` resolves X's tailnet IPv4 and verifies the app is
     listening locally (so a bad target fails fast, not at first request);
   - the gateway allocates a collision-free external port (skipping ports
     with live OS bindings), records the lease, and binds it immediately;
   - the response includes the stable `url`.
3. The registrar (or the gateway's probe, when the target is on another
   host) keeps the lease alive while the app is healthy.
4. App stops → probes fail → no renewal → TTL expiry → port released.
5. `herdr-eng devfabric close <lease_id>` releases a port immediately.

## Degraded / local mode

If no gateway is configured (`fabric.gateway_url` /
`HERDR_ENGINEERING_FABRIC_URL` unset) or unreachable, registrar commands fall
back to the local registry and `devfabric serve` (per-host forwarder) still
works standalone. A lease made this way is **not** reachable via the fleet
front door until the gateway is up; the CLI says so explicitly. Local-mode
writes land in the same store file, and a running gateway picks them up at
its next probe cycle via store reload.

## Security

- Router and control API bind to loopback/tailnet only — no public listeners;
  public binds are refused (`DevServiceForwarder` and `GatewayRouter` both
  check).
- Bearer token on the control API when configured; the token lives only in
  the environment or a machine-local gitignored file.
- The gateway is an operator service: start/stop is an explicit, logged
  action; leases are explicit; nothing is exposed implicitly.

## Operations

```bash
# start the gateway (on bender; control :29999, router on loopback+tailnet)
herdr-eng devfabric gateway

# register an app running on this machine (tailnet-reachable)
herdr-eng devfabric register --machine beast --host auto --port 5678

# point commands at the gateway without editing config
HERDR_ENGINEERING_FABRIC_URL=http://bender.tail0c50da.ts.net:29999 \
  herdr-eng devfabric list

# stop the gateway (leases survive in the store and are re-bound on restart)
kill <gw-pid>   # SIGTERM is handled: router unbinds, lock releases
```

The gateway is a candidate for fleet Ansible deployment (systemd unit) once
the host choice is finalized (bender today; a dedicated node later — the
service is stateless except for the store directory).

## Persistent private web UI (gateway host)

The fleet-wide private control surface (`herdr-eng web`, spec 0060) follows
the same one-gateway model: it runs **persistently on the designated gateway
host** (bender today) as a systemd **user** service, and is exposed tailnet-only
via `tailscale serve`.

```
 tailnet (any device: desktop / phone / iPad)
      │  https://<gateway-magicdns>/   (tailscale serve, HTTPS)
      ▼
 gateway host (bender):  herdr-eng-web.service (systemd user unit)
      │  herdr-eng web --host 127.0.0.1 --port 8787
      ▼
 loopback only 127.0.0.1:8787   (private-by-default invariant)
```

- **Private-by-default invariant.** `web.py` binds loopback only and its
  defaults are **not** changed. The systemd unit additionally pins
  `--host 127.0.0.1` so a machine-local config can never turn it into a public
  listener. There is no public listener, ever.
- **One gateway host.** Only the designated gateway host (bender today) runs
  the service. Other fleet hosts reach the UI via the stable
  `https://<gateway-magicdns>` URL or an SSH tunnel
  (`ssh -L 8787:127.0.0.1:8787 <gateway>`), never by running their own
  public listener.
- **Persistent.** A systemd user unit (`scripts/herdr-eng-web.service`) keeps it
  running across reboots/logout via `loginctl enable-linger`.
- **Tailscale serve.** `tailscale serve --bg 8787` maps `https://<gateway-magicdns>`
  → `127.0.0.1:8787`. The serve feature must be enabled once on the tailnet
  (admin console); until then the setup script prints the exact command and
  continues gracefully.

### Deploy

```bash
# On the gateway host (bender), from a checkout:
scripts/web-serve.sh          # idempotent; enables linger, installs+starts the
                              # user unit, then `tailscale serve --bg 8787`
```

The same deployment is driven by Ansible on the gateway host
(`ansible/playbooks/site.yml`, the "Deploy private web UI ..." block, gated by
`herdr_engineering_gateway_host`). It copies the unit + `scripts/web-serve.sh`
to the host and runs the script as the fleet user. See the README section
"Persistent private web UI (gateway)" for the full model and `ansible/README.md`
for fleet convergence notes.

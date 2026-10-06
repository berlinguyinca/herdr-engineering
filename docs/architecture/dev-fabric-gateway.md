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

## Auto-registration agent (zero manual steps)

Manual `devfabric register` is the explicit path; the **agent** makes it
automatic. A per-host service (`herdr-eng devfabric agent`, deployed by
Ansible on every fleet host as a systemd user unit) runs one scan cycle every
few seconds:

1. **Detect** — enumerate listening TCP ports (`ss` on Linux, `lsof` on
   macOS). A port is a candidate only when it looks like a *dev* web service:
   non-privileged (`>= 1024`, or a common web port), below the OS ephemeral
   range, not a well-known infrastructure daemon (CUPS, restic, Prometheus,
   exporters, …), and it answers an HTTP `GET /` with a status `< 500`. This
   noise filter keeps system daemons and debug/agent endpoints out of the
   fabric; every threshold is configurable (`fabric.agent_*`).
2. **Adopt** — register the service with the gateway. If the app is reachable
   on the host's tailnet IP the target is registered directly; if it is
   loopback-only the agent starts a tailnet-bound **forwarder** in front of it
   (same byte pipe as the router, bound to the tailnet IP only) and registers
   the forwarder's port. Either way the app appears at a stable `dev:<port>`.
3. **Keepalive** — tracked leases are heartbeated at half the gateway TTL; a
   lease the gateway dropped (restart or TTL lapse) is transparently
   re-registered, so the mapping never goes stale.
4. **Release** — when a tracked service stops, its lease is closed and its
   forwarder stopped, freeing the `dev:<port>` port fleet-wide.

The agent’s local store (`state/fabric-agent.json`) is what it has already
adopted; it is the single writer and re-derives its forwarder set on restart.
Because every host runs an agent, **starting a web service on any fleet host
makes it appear in the gateway host's web UI and at `dev:<port>` with no
manual registration.**

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

# run the auto-registration agent on this host (normally a systemd unit):
# detects local web services and keeps the gateway's lease set in sync\r
herdr-eng devfabric agent            # foreground loop (Ctrl-C to stop)
herdr-eng devfabric agent --once     # one scan cycle (cron / smoke test)

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

## Shared `dev.lan` domain (Option B)

The front door is reachable from every tailnet device via one stable name,
**`dev.lan`**, independent of where a service runs. Because `.local` is the
mDNS/Bonjour reserved namespace, **Tailscale MagicDNS will not serve it**, so
we run our own authoritative nameserver and use Tailscale **split-DNS**:

- A **CoreDNS** container on the master host (bender) is authoritative for the
  `dev.lan` namespace and answers `dev.lan` / `*.dev.lan` with the master
  host's tailnet IPv4.
- Tailscale admin console → **DNS → Nameservers** adds that IP as a nameserver
  **restricted to the `dev.lan` domain** (split-DNS). Tailscale sends only
  `dev.lan` queries there; every other name keeps using MagicDNS. Devices on
  the tailnet that accept Tailscale DNS (desktop, macOS, iOS, Android) then
  resolve `dev.lan` fleet-wide.
- The fabric gateway advertises `url_base: dev.lan`, so every lease is
  `http://dev.lan:<port>`; the router binds each external port on the
  gateway and proxies to the owning host.

The whole front door runs as one Docker Compose project on the master host —
CoreDNS + fabric gateway + private web UI — in **`deploy/fabric-stack/`**
(see its `README.md`). All services use `network_mode: host` (the gateway must
bind the tailnet IP + the whole `18000–28999` range and route to other hosts;
CoreDNS must answer on the tailnet IP:53; the web UI stays loopback-only).
CoreDNS binds only `127.0.0.1` + the tailnet IP to avoid colliding with
systemd-resolved (`127.0.0.53/127.0.0.54`) and libvirt dnsmasq
(`192.168.122.1`) on port 53.

We use **`dev.lan`** (not `.local`): `.local` is the mDNS/Bonjour reserved
namespace, so Tailscale MagicDNS will not serve it and the admin console
refuses it as a custom namespace. `dev.lan` is not reserved, so Tailscale
accepts it as a split-DNS namespace and it stays private to the tailnet.

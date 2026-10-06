# HerdR Engineering master-host stack (`dev.lan` front door)

Runs the entire fleet front door as **one Docker Compose project** on the
master/gateway host (bender today). From any Tailscale device you reach any
fleet service at `http://dev.lan:<port>` — regardless of which host actually
runs the service.

```
 any tailnet device (desktop / phone / iPad)
      │  http://dev.lan:18052            (CoreDNS split-DNS -> gateway IP)
      ▼
 master host (bender):  dev.lan resolves to 100.104.39.6
      ├─ herdr-coredns      CoreDNS, answers dev.lan + *.dev.lan
      └─ herdr-fabric-gateway  binds 18000-28999, proxies to the owning host
             └─ target: beast:5678 / mac:8080 / bender:3000 (any fleet host)
```

## What this stack contains

| Service | Image | Role |
|---|---|---|
| `coredns` | `coredns/coredns:1.11.4` | Private nameserver for the shared `dev.lan` domain (Option B split-DNS). |
| `fabric-gateway` | `herdr-engineering:0.1.0` (built) | The `dev:<port>` front door (spec 0110 #3): control API + per-port router + probe loop. |
| `herdr-web` | `herdr-engineering:0.1.0` (built) | Private web/mobile control surface (spec 0060), loopback-only, tailnet-only via `tailscale serve`. |

The per-host **auto-registration agent** is intentionally *not* containerized:
it must see host listeners via `ss`/`lsof`, so it stays a host service on
every fleet host (systemd user unit on Linux, launchd LaunchAgent on macOS),
pointing at the gateway via `HERDR_ENGINEERING_FABRIC_URL=http://dev.lan:29999`.
Join a machine with `scripts/fabric-agent.sh` (Linux) or
`scripts/fabric-agent-macos.sh` (macOS); leave with `scripts/fabric-offboard.sh`.
See [`docs/operations/fleet-lifecycle.md`](../../docs/operations/fleet-lifecycle.md)
for the full multi-operator walkthrough.

## Why `dev.lan` (Option B)

`.local` is the mDNS/Bonjour reserved namespace, so **Tailscale MagicDNS will
not serve it** and the admin console refuses it as a custom namespace. We
chose **`dev.lan`** instead: it is not reserved, so Tailscale accepts it as a
split-DNS namespace and it is still private to the tailnet. We run our own
authoritative nameserver (CoreDNS) and use Tailscale **split-DNS**: Tailscale
sends only the `dev.lan` namespace to CoreDNS; everything else keeps using
MagicDNS. Every device on the tailnet that accepts Tailscale DNS — including
phones and iPads — then resolves `dev.lan` automatically.

## Prerequisites (on the master host)

- Docker with Compose v2 (`docker compose version`)
- `tailscale` CLI, tailnet up
- (recommended) this repo checked out

## One-time Tailscale admin-console step (external, ~2 min)

This is the only step that cannot be automated from this host; it is an
admin-console action:

1. Open the Tailscale **admin console → DNS → Nameservers**.
2. **Add nameserver** → enter the master host's tailnet IPv4
   (e.g. `100.104.39.6`).
3. Enable **"Restrict to domain"** and set it to `dev.lan`.
4. Save.

Tailscale will now forward only `*.dev.lan` queries to CoreDNS on the master
host.

## Deploy

```bash
cd herdr-engineering
scripts/fabric-stack.sh            # detect IP, render CoreDNS config, build + up
```

The script writes `deploy/fabric-stack/.env` with `GATEWAY_TAILNET_IP` (from
`tailscale ip -4`), renders `coredns/generated/*` from the `.tmpl` files, builds
the `herdr-engineering` image, and starts the stack.

To replace the previously host-run gateway process + the `herdr-eng-web`
systemd unit (they would conflict on ports `29999` / `8787` / `18000+`):

```bash
scripts/fabric-stack.sh up-replace
```

Other commands: `down`, `status`, `stop-old`.

### Manual (without the script)

```bash
cd deploy/fabric-stack
cp .env.example .env
# set GATEWAY_TAILNET_IP=<tailnet ip>
# render CoreDNS config:
mkdir -p coredns/generated
sed 's/{{GATEWAY_TAILNET_IP}}/<tailnet ip>/g' coredns/Corefile.tmpl   > coredns/generated/Corefile
sed 's/{{GATEWAY_TAILNET_IP}}/<tailnet ip>/g' coredns/dev.lan.zone.tmpl > coredns/generated/dev.lan.zone
docker compose up -d --build
```

## Verify

```bash
# DNS: dev.lan resolves to the gateway IP (loopback + tailnet)
dig @127.0.0.1 dev.lan A                 # -> 100.104.39.6
dig @100.104.39.6 foo.dev.lan A          # -> 100.104.39.6

# Gateway control plane is up and advertises dev.lan
curl -s http://127.0.0.1:29999/healthz     # -> {"url_base":"dev.lan", ...}

# From any device on the tailnet, once split-DNS is configured:
curl http://dev.lan:18052                # reaches the leased service
```

## Design notes

- **`network_mode: host` everywhere** — required. The gateway must bind the
  host's tailnet IP and the whole `18000–28999` dev range, and reach other
  tailnet hosts; CoreDNS must answer on the host's tailnet IP:53; the web UI
  binds loopback only (private-by-default invariant) and is exposed via
  `tailscale serve --bg 8787`. No port remapping is done.
- **CoreDNS binds only `127.0.0.1` + the tailnet IP** (not `0.0.0.0`): the host
  already runs systemd-resolved on `127.0.0.53/127.0.0.54` and a libvirt
  dnsmasq on `192.168.122.1`, all on port 53.
- **`url_base` is set explicitly to `dev.lan`** in `compose.yaml`/`config.yaml`.
  The container image has no `tailscale` CLI, so auto-discovery
  (`magicdns_name`/`tailnet_ipv4`) is not used; explicit bind + url_base are
  passed on the command line.
- **`tailscale` CLI is not in the image** on purpose — keeps it slim and
  deterministic; the gateway and web only need explicit host/url config.
- **Gateway store** (`leases.json`, `gateway.lock`) lives in the `herdr-state`
  named volume. Leases survive gateway restarts.
- **HTTPS**: the router is a byte-transparent TCP proxy, so
  `https://dev.lan:<port>` works for any TLS-speaking target on a leased
  port. Path-based routing on `:443` is a separate mechanism (`tailscale serve`).

## Troubleshooting

- **`dev.lan` won't resolve from another device** → the Tailscale split-DNS
  nameserver is not configured (admin console step) or the device isn't using
  Tailscale DNS. Check `tailscale dns status` on the device.
- **Gateway won't start** → something already holds `29999` or the dev range
  (an old host-run gateway). Run `scripts/fabric-stack.sh up-replace` to stop
  the old process, or `pkill -f 'herdr-eng devfabric gateway'`.
- **CoreDNS fails to bind** → another service owns `:53` on `127.0.0.1` or the
  tailnet IP. Check `ss -ltn | grep ':53'`; the `bind` directive in
  `coredns/generated/Corefile` is intentional.
- **`dev.lan` won't resolve on the gateway itself** → the host's own resolver
  may not route `dev.lan` to CoreDNS; query CoreDNS directly with
  `dig @127.0.0.1 dev.lan`, and confirm the admin-console nameserver is
  restricted to `dev.lan`.

# Same-origin app edge (path-routing forwarder)

A dev app is often **two or more services** — e.g. a UI on one port and an API
on another — but the browser wants **one origin**. The dev fabric is, by design,
an **L4 byte pipe** (one external `dev:<port>` → one `target_host:target_port`);
it never parses requests (that's how WebSocket/SSE/HMR/HTTPS/TCP all flow
through it safely). The **same-origin edge** adds the missing **L7 hop** without
changing that design.

## What it does
You register a lease with **routes** — path prefixes mapped to backends — plus a
**catch-all** (the lease's normal `target_host`/`target_port`). The edge:

1. binds the origin's external port (a normal lease → `dev:<port>`, Tailscale,
   and health all apply unchanged);
2. **peeks the first HTTP request line** to read the path;
3. picks the backend by **longest-prefix match** (no match, or a non-HTTP
   connection → the catch-all);
4. **byte-pipes** the connection to that backend (reusing the fabric's
   transparent pump, so WS/SSE/HMR still work).

Non-HTTP connections (e.g. a raw TLS ClientHello) can't have their path peeked,
so they fall back to the catch-all instead of hanging. On the private tailnet the
origin is plain HTTP; if you ever need TLS termination, front a Caddy/nginx in
front (the edge is deliberately L7-lite, not a full TLS terminator).

## Why not make the fabric itself L7?
The fabric's value is being protocol-transparent and simple (a port→process
forwarder with auto-discovery). Path routing needs per-app knowledge ("`/api`
belongs to the API backend"), which is app-specific. So the routing lives in the
**lease** (declared by the app), and the edge executes it — the fabric stays L4.

## Declare one
The lease's own `--host`/`--port` is the catch-all; each `--route` is a
path-prefix override. Longest prefix wins.

```bash
# Augur: origin 3011 -> UI (frontend:3000) by default, /api -> api:8300
herdr-eng devfabric register \
  --machine <machine> --host <tailnet-ip-or-name> --port 3000 \
  --preferred-port 3011 \
  --route /api=<tailnet-ip-or-name>:8300 \
  --repo augur --label "augur console"

# then forward it (per-host mode) — or the central gateway does it automatically
herdr-eng devfabric serve --lease <lease-id>
```

Result: `http://dev:<3011>` serves the UI, and `http://dev:<3011>/api/...` hits
the API — **one origin**, no CORS, no hardcoding a backend host in the browser.

## API
- `Route(prefix, host, port)` — `herdr_engineering.contracts`. Prefix is
  normalized (`/api`, `/`); the lease's target is the catch-all.
- `DevServiceLease.routes: list[Route] | None` — optional; `None` = the usual
  1:1 service (fully backward compatible).
- `DevServiceRegistry.register(..., routes=[...])` — stores routes on the lease
  (accepts `Route` objects or plain dicts; the store round-trips them).
- `OriginEdge(lease, bind_host, private_only)` — the multi-target forwarder
  (`start`/`stop`/`bound`, same contract as `DevServiceForwarder`).
- `forwarder_for(lease, ...)` — returns `OriginEdge` when the lease has routes,
  else `DevServiceForwarder`.
- `GatewayRouter.bind(port, host, port, routes=...)` — the central gateway routes
  a lease's origin by path (via `op_register` reading `body["routes"]`).
- `route_and_pipe`, `match_target`, `path_matches`, `_peek_request_path`,
  `prefixed_pipe` — the routing core in `herdr_engineering.devfabric`.

## Guarantees
- **Backward compatible**: a lease with no `routes` behaves exactly as before
  (plain 1:1 forwarding). All existing fabric behavior/tests are unchanged.
- **No new runtime dependency**: stdlib sockets only, reusing the existing
  byte pump.
- **Private-only** bind, like the rest of the fabric.

## Tests
`tests/test_edge.py`: path matching, request-line peek, target selection, the
lease data model (round-trip, backward compat), forwarder dispatch, and a **live
loopback test** proving `/api/*` and `/` reach different backends through the
edge.

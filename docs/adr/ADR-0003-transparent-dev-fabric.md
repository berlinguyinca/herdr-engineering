# ADR-0003 — One `dev` service name plus leased ports

**Status:** Accepted — 2026-09-23

## Decision

Expose ephemeral development services through one stable, private logical service name (`dev`) and a cluster-wide leased external port, rather than creating a permanent DNS name for every agent/worktree.

Conceptually:

```text
http://dev:18431 -> bender / worktree A / localhost:5173
http://dev:18432 -> fry    / worktree B / localhost:3000
```

Use Tailscale's current private service/routing primitives where they fit and keep HerdR Engineering responsible for authenticated lease allocation, ownership and reconciliation.

## Why

Dozens of concurrent agent worktrees make human-chosen hostnames unmanageable. Existing skills already launch servers on free local ports; the missing layer is stable fleet-wide reachability from any tailnet device.

## Consequences

- Do not force agents to choose unique names.
- Leases need atomic allocation, TTL/renewal, health and crash reconciliation.
- Browser/HMR/WebSocket/SSE behavior becomes an acceptance requirement, not merely plain HTTP.

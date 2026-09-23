# 0110 — Transparent Development Service Fabric

## Goal

Let dozens of agent worktrees launch web/dev servers on any participating machine and make each reachable from every tailnet device using one logical `dev:<leased-port>` address, without human-managed hostnames.

## Existing launch behavior

Do not replace the user's existing skill/process that chooses a random free local port and launches the app. HerdR Engineering registers/discovers that resulting local endpoint and adds the fleet-wide reachability layer.

## Components

1. **Lease registry** implementing `DevServiceLease`.
2. **Host registrar/agent** that verifies target process/listener ownership and renews health.
3. **Private router/service front door** associated with the stable Tailscale `dev` service name.
4. **Herdr UI integration** showing preview address, owner session/worktree and health.
5. **Cleanup/reconciler** for process exit, machine loss, service restart and orphaned leases.

## Allocation

- Use an administratively configurable external port range.
- Allocation must be atomic under concurrent requests across hosts.
- Registration is idempotent for the same process/session identity.
- Do not reassign an active or uncertain lease to another worktree.
- TTL + heartbeat + explicit close; crash reconciliation prevents permanent leaks.

## Protocol correctness

Validate:

- HTTP/1.1 and normal browser navigation;
- HTTPS when enabled;
- WebSocket upgrades;
- SSE;
- Vite/Next/React hot-module reload patterns;
- cookies/headers/path/query preservation;
- streaming/chunked responses;
- declared generic TCP pass-through.

## Security

- Tailnet/private exposure only by default.
- Route only to registered target machine/loopback endpoint.
- Reject arbitrary user-supplied target hosts outside policy.
- Audit lease create/renew/close and access-control decisions.
- Optional project/user policy can restrict who may open/control a service.

## Exit gate

A stress test concurrently registers services on at least three hosts, allocates collision-free ports, reaches each from desktop and mobile through `dev:<port>`, passes WebSocket/SSE/HMR tests, then kills processes/hosts and demonstrates correct stale-route cleanup with no cross-routing.

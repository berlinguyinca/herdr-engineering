# Contract: DevServiceLease

A `DevServiceLease` makes an ephemeral server launched on any HerdR-managed machine reachable through the stable logical `dev` service without giving every worktree a permanent hostname.

```yaml
dev_service_lease:
  id: "lease_01..."
  external_port: 18431              # unique across the logical dev fabric
  protocol: "http|https|tcp"
  target:
    machine_id: "fry"
    host: "127.0.0.1"               # normally loopback on target host
    port: 5173
  owner:
    user_id: "..."
    herdr_session_id: "..."
    repository: "owner/repo"
    worktree_id: "wt_..."
    process_id: "..."               # optional/native process identity
  label: "feature-auth-ui"
  state: "pending|active|draining|expired|failed"
  created_at: "RFC3339"
  renewed_at: "RFC3339"
  expires_at: "RFC3339"
  health:
    last_ok_at: "RFC3339"
    last_error: null
```

## Addressing model

The user-facing concept is:

```text
http://dev:<external_port>
```

or the HTTPS equivalent when configured. `dev` is a single stable tailnet-only service name. The port distinguishes dozens of concurrent ephemeral worktree servers.

## Invariants

- External ports are allocated atomically and are unique among active/draining leases.
- Registration is idempotent for a stable owner/process identity.
- A lease never silently points to a different owner after process death.
- Stale ownership expires or enters failed state before the port can be reused.
- Only authenticated tailnet/private callers can reach the routing plane by default.
- WebSocket, SSE and hot-module-reload upgrade semantics must pass through unchanged for HTTP services.
- Generic TCP is allowed only for explicitly declared service types/policies.
- Lease state survives control-plane restart and reconciles against actual process/host health.
- The registry is not coupled to one specific host and must tolerate an unreachable machine.

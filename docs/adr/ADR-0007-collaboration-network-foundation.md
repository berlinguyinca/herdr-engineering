# ADR-0007: Collaboration network foundation (HCN P1)

Status: accepted for the flagged P1 slice, 2026-10-21.

## Decisions
1. **In-process module, not a gateway.** `control_plane/collab.py` runs inside `ControlPlaneServer`. A separate gateway would duplicate the sink, the limiter and deployment; revisit for federation.
2. **Own authenticator, behind an interface.** The existing `X-API-Key` roles are shared secrets with no identity and are denied on `/api/v1/collab/*`. `UserAuthenticator` resolves a bearer token to a user id; the default is a hashed static registry. OIDC/MFA replaces it without touching authorization.
3. **Event-sourced, same durable table.** All state is a fold of `collab` events persisted via `DurableEventSink.record_required` into `fabric_events`. They never enter `repo._events`, so the global SSE, `/activity` and generic events endpoint cannot leak them.
4. **Transport.** REST + per-room SSE with per-tick re-authorization. Agents poll/stream outbound; no inbound ports. Peer messages are never routed to `herdr agent prompt`.
5. **Authorization** = owner membership ∩ agent grant ∩ role ceiling ∩ expiry, evaluated on every request, default deny, 404 for non-members.
6. **Artifacts, A2A, MCP** are P2/P3 and reuse `ArtifactStore`, `reviews.py` and the same API.

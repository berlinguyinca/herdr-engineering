# Provider-neutral Public Service Gateway

## Non-negotiable separation
Internal connectivity (Tailscale/WireGuard/LAN/etc.) != public exposure transport (Funnel/Cloudflare/VPS/direct/K8s) != Herdr authorization. Internal service identity and artifact ID must be independent of public hostnames. No required public provider for private deployments.

## Components
- Publication Controller stores policy/desired state, approval, providers and audit, reconciles actual state.
- Provider Adapter only manages transport/routes/cert capabilities; MUST NOT make authorization decisions.
- Public Gateway handles public request enforcement (authentication, exact host/path/method, rate and concurrency, response headers, isolation) and reads narrowed delivery-only policy; no generic access to control-plane APIs.
- Static delivery: copy approved immutable artifact rendition into dedicated public-delivery namespace/bucket or scoped edge cache, never expose private origin bucket/credentials. Revocation invalidates gateway accesses and controlled caches; consumers may retain previous copies.
- Live delivery: route to allowlisted fabric service ID via narrowed proxy/connector; do not accept user-supplied origin URL/IP. Host resolution revalidates service identity and approved route.

## Provider interface
`capabilities()`, `validate_config()`, `prepare(spec)`, `expose(spec)`, `observed_state(id)`, `revoke(id)`, `reconcile(desired)`; each operation idempotent, scoped credentials, explicit errors and capability enum (https, websocket, custom-domain, wildcard, cert management, edge auth, route limits, bandwidth). Provider status reported, never presumed.

## Adapter order
1. Local fake/provider conformance suite and private-only provider (feature absent gracefully).
2. Tailscale Funnel adapter compatible with existing private tailnet, no unapproved Serve/Funnel changes. Be explicit that Funnel public clients do not provide tailnet identity.
3. Cloudflare Tunnel + self-hosted HTTPS edge (Caddy/Traefik, WireGuard/VPS) adapters; opt-in.
4. K8s Gateway API adapter; use native HTTPRoute instead of bespoke K8s routing objects.

## Publication record
`publication_id`, resource type/id/version, requester, approver, audience `unlisted_link|authenticated_guest|public_discoverable`, expiry, domain/path, provider ID/config reference, methods, websocket bool, quotas, effective policy revision, lifecycle, observed endpoint, audit and revocation timestamp. Separate publication-level policy from resource access (both evaluated). Publish requests from agents require authorized human approval; issuer identity from trusted auth, never JSON claimed identity.

## Security specifics
The public gateway MUST NOT share Herdr cookies, permissive CORS or credential scope with generated sites. HTML/SVG on distinct origin and sandboxed; strict CSP, nosniff, attachment for risky formats; sanitize previews. Block arbitrary destinations, DNS rebinding, IPv6 private/metadata targets, redirect-to-private, wildcard route overlap, path traversal, Host header abuse. Restrict method/body and WebSocket separately. Set exact timeouts, rate/concurrency/bandwidth/egress quotas; access logs privacy and retention.

## Emergency controls
Global pause enforced by public data plane; even if control plane is unavailable. Publication revoke must fail closed and be tested under provider disconnect. Understand external provider orphan routes: data plane must independently deny requests even when adapter deletion fails. Providers need revocable short-lived credentials or reconciliation alarms.

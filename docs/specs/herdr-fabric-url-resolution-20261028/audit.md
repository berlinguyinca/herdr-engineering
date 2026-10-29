# Fabric URL Resolution and HTTPS Gateway — Phase 0 audit

Date: 2026-10-28 · Repo: `berlinguyinca/herdr-engineering` @ `410b28d` (origin/main)
Worked in an isolated worktree (`feat/fabric-url-resolution`); the deployment checkout
`~/herdr-engineering-repo` and the dirty `~/IdeaProjects/herdr-engineering` were not touched.
Pi Engineering (`berlinguyinca/pi-engineering`) was inspected read-only; no URL rendering lives there.

## What already exists (extend, do not duplicate)

| Concern | Where | State |
|---|---|---|
| Lease registry (collision-free ports, TTL, heartbeat, health) | `devfabric.py` `DevServiceRegistry` | Solid. Prefers external port == target port. |
| Gateway + control API | `fabric.py` `FabricGateway` (`:29999`, bearer token) | Control plane only is authenticated. |
| Per-lease listeners | `devfabric.py` `GatewayRouter`/`OriginEdge`, `proxy_pair`, `route_and_pipe` | Plain-TCP byte proxy; HTTP/WS/SSE/HMR pass through. |
| HTTP head rewrite | `urlrewrite.py` `rewrite_response_head`, `force_close` | Location/ACAO/Set-Cookie only; keeps `http`. |
| Loopback URL rewrite | `urlrewrite.py` `resolve_loopback`/`rewrite_url`/`rewrite_text`; `FabricGateway.op_resolve_loopback`/`op_rewrite` | Plain-text, http only. See gaps. |
| DNS | `deploy/fabric-stack/coredns/` | `dev.lan`, `*.dev.lan` → master tailnet IP; Tailscale split-DNS. |
| Browser linkifier | `packages/conversation-markdown.js` (`linkifyBare`, `markdownLink`, `references.resolve`) | Single safe tokenizer; code spans/fences never linkify. |
| Services page | `packages/services-view.js`, `ENTITY_ROUTES.service` (`#/services/<id>`) | Phase 4 "Fabric Services view" extends this. |
| Funnel exposure | `exposure_tailscale.py`, `exposure_cloudflare.py`, `public_gateway*.py` | Public-internet publication; wrong layer for private fabric HTTPS. Not reused for TLS. |

## Gaps found (each verified in source)

1. **No TLS anywhere in the data plane.** `stable_url()` hardcodes `http://`.
2. **`resolve_loopback` skips the primary case.** If a lease exists on `from_machine` it returns `None`
   (assumes the reader is on that host). A dashboard reader is a browser (maybe a phone), so
   "localhost URL on the local agent host" never rewrites.
3. **`rewrite_url` keeps the scheme and assumes the port**: external port only *prefers* the target port.
4. **Two URL detectors would disagree**: Python `_URL_RE` vs the JS tokenizer/`splitTrailingPunctuation`.
5. **`op_register` does not validate `target_host`.** Any bearer-token holder can lease
   `169.254.169.254` or any internal address: the gateway is an SSRF-capable open proxy today.
6. **Data-plane listeners are unauthenticated.** Only the control API checks a token; any tailnet peer can
   reach every leased port. Acceptance test 13 and "no unauthorized exposure" cannot pass as-is.
7. **Single-writer gateway** (`gateway.lock`) + CoreDNS answering with one IP: no failover.
8. **No source-network-namespace field** on leases (host only), so container-isolated sources cannot be told apart.

## Prior art / overlap
`gh pr list` for tls/https/urlrewrite/dev.lan: only merged work (#46, #117 forward-Host, #138, #47). No open
overlapping PR. `herdr-artifact-fabric-publication-20261025` is about artifact publication, not URLs.

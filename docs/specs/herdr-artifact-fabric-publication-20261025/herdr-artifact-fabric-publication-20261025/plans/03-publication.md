# Work package 3 — public gateway and Tailscale transport

- Design provider-neutral publication API and state store; human-only approval; separate fabric visibility from public sharing. Use policy layering, origin pinning, expiry, audit.
- Create isolated public data plane/gateway with deny-by-default route table and health; static replicas on separate namespace; public HTML sandbox under distinct origin. Add revoke and global kill switch.
- Provider protocol: fake/local adapter, Tailscale Funnel as first opt-in real adapter; no Tailscale dependency for private-only and no unapproved tailnet reconfiguration.
- Publication list, request review, provider caps, approved state, revoke and drift notifications in existing dashboard.
- Run negative tests (SSRF, arbitrary service ID/URL, permission bypass, stale provider route, revoke with disconnected control plane, anonymous public guest identity). Do not publish private source or control plane.

Done when: approved pinned artifact shared to public URL; privacy/expiry/revoke confirmed against live gateway; private-only mode remains functional.

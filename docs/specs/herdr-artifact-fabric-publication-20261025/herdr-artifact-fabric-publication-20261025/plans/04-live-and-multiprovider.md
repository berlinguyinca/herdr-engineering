# Work package 4 — live services and additional exposure transports

- Extend gateway with allowlisted service IDs and exact method/path upgrades; authenticated restricted edge connector. No arbitrary origin hosts or broad subnet routing.
- Add provider adapter(s) for self-hosted reverse proxy/VPS, Cloudflare Tunnel, and K8s Gateway API where warranted. Each conforms to shared fake/real tests; private overlay may be Tailscale, WireGuard, other.
- Implement custom domain config, cert renewal observability, quotas, bandwidth budgets, outage/drift reconciliation, kill switch and rollback.
- Add collections, dependency-aware stale/regeneration notices, renderer capability discovery, more artifact profiles and template catalog, anchored to actual usage.

Done when: a preview service can be published temporarily through one provider and revoked; gateway continues restricting access on provider/controller failures; adapter swap does not alter artifact ID.

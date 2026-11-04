# Threat model and trust boundaries

Assets: private RustFS objects, agent credentials, source trees, mission metadata, auth sessions, public delivery snapshots, build workers, gateway config, certificate keys.
Actors: public anonymous visitor; invited guest; hostile external agent; compromised fabric worker; malicious document; operator with publication rights; third-party exposure provider.

Trust boundaries: public browser → internet edge → public gateway → narrow artifact delivery/live proxy; agent → authenticated control plane → storage; rendering sandbox → immutable outputs. No implicit identity transfer from Tailscale/Funnel or from trusted browser headers.

Critical abuse cases and defenses:
1. Unauthorized artifact retrieval by guessing IDs or hash: ACL enforced before returning existence or presigned URLs; hash dedup never equates authorization.
2. Direct S3 access: private buckets and network ACL; gateway has read only to approved public replicas or scoped immutable paths.
3. Cross-origin generated HTML: separate origin, CSP, iframe sandbox, isolated cookies; no unsafe parent scripting.
4. Malicious PDF/DOCX/SVG/image: content sniffing, scanner, resource-limited rendering and sanitization.
5. SSRF via publication origin: origin may only be registered service ID; re-resolve through authenticated service discovery; block localhost/link-local/RFC1918/IPv6 private/metadata and redirect re-resolution.
6. Permission confused deputy: never accept owner/principal/approval fields from untrusted agent payload; derive from auth and record human approver.
7. Arbitrary code in build: isolated sandbox, no host mounts or network by default; separate least-privilege identity; worker limits and secret redaction.
8. Concurrent edit rollback: optimistic locking, immutable versions and idempotency.
9. Revocation while controller down: public gateway deny independent of provider route; fail closed on stale policy; limited offline cache lifetime; alert on provider drift.
10. Data leaks through search/thumbnails/previews: per-user filtering also on indexing, derivative storage and cache keys.
11. Denial of service/cost explosion: request limits, quotas, pagination, bounded queues, per-owner budgets and bandwidth alerts.
12. Expired guest access and token disclosure: secrets hashed at rest, rotate on demand, expiry and revocation; logs never expose unlisted tokens.

Required review gates: threat model signoff before public edge deployment, secrets check on all publication paths, negative authorization tests across tenants, independent gateway security review, external API penetration tests as resources permit.

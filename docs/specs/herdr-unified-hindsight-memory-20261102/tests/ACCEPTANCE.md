# Acceptance and verification matrix

1. **Reuse audit** — agent enumerates exact existing implementations and commit SHAs before edits; no duplicate service or DB added.
2. **Cross-host** — authorized Pi on host A saves verified test memory; Claude on host B retrieves same ID/revision in a fresh session; Codex and OpenCode compatible adapters retrieve it; all traced in Herdr event metadata. If live hosts unavailable, report simulated only and block release.
3. **Isolation** — project B and personal-assistant worker cannot see project A private memory; attempts return 403 or filtered results without leaking existence.
4. **Correction** — update/retraction invalidates caches; old revision not selected by default; as-of historical view only with permission.
5. **Durability** — lost network, process crash, full queue, duplicate retry, outbox replay: no acknowledged-write loss or duplicate retained record.
6. **Read limits** — 250ms Pi automatic budget preserved where applicable; invalid auth and outage clear relevant cache and show degraded status.
7. **Evidence** — unverified hypothesis not promoted to global knowledge; verified PR/test refs linked; no hidden reasoning or raw secrets.
8. **Handoffs** — old handoff packages parse; new memory refs survive offline handoff and resume; checkpoint state accurate.
9. **Installer** — repeated install no unintended diff; user configs preserved; harness not installed is skipped; foreign configs reported rather than overwritten; managed uninstall reversible.
10. **Dashboard** — authorized search and detail; correct paging, loading, offline, no results, forbidden; mobile keyboard accessibility; no dummy statistics.
11. **Deployment** — canary on one idle host, version pinned, backup and rollback tested; never change active sessions; fleet apply blocked until safety features verified.
12. **Benchmark** — baseline controlled tasks with and without memory, track task quality, token use, latency, false retrieval and repeat failures; report actual values.

Every test report must include mode: mocked / local integration / real cross-host / production smoke. All failures and skips recorded; do not claim rollout success on mocks only.

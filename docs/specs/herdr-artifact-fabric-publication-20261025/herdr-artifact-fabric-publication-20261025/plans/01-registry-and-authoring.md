# Work package 1 — artifact registry, durable creation and edits

1. Extend DB schema safely with versions, blobs, ACLs, metadata provenance, collections, edit jobs and audit. Add backfill and reversible/forward-compatible rollout.
2. Evolve current ArtifactStore: streaming upload, verified hashes, typed output, CAS version commits, idempotency, scoped download and materialization. Preserve existing methods and tests; factor adapters rather than parallel store.
3. Add canonical artifact URI and API; persist bindings to missions/sessions/rooms/hosts/commits/issues, indexed authorized search.
4. Add resilient generator job worker profile for web and Markdown first. Preserve source archive/assets/build metadata and immutable output, test sandbox. Implement CLI and shared `herdr-artifact` skill with checkout/edit/publish/history; use existing scheduler + host denylist.
5. Add cross-host end-to-end test and concurrency tests; no public exposure in this slice.

Done when: A on host X creates, B on Y edits, C reviews version history; parent host offline; source+outputs preserved, conflicts safe.

# Shared contract v1 (implementation brief for all phase PRs)
Authoritative source: `herdr-memory` CLI JSON (stdout). All other repos consume this; they do not talk to Hindsight directly.

## Scopes -> banks
`project` -> existing `<project>-facts` / `<project>-summaries` (unchanged). `global` -> `herdr-global`. `infra` -> `herdr-infra`. `mission:<id>` -> `<project>-mission-<id>` (id `[a-z0-9][a-z0-9_-]{0,63}`). Session memory is NOT stored in Hindsight by default. Personal/Rakazo banks are never addressable. Access is deny-by-default: a scope is usable only if config has a credential for it; client-side checks are workflow safeguards, the server token is the authority.

## Config (backward compatible, `~/.config/herdr-memory/config.json`)
Existing keys unchanged. Added optional: `scopes: {global:{credentials:{<harness>:path}}, infra:{...}}`, per-project `mission_scopes: true`.

## CLI additions (all print one JSON object; non-zero exit on error)
- `recall QUERY [--scope project|global|infra|mission:ID ...] [--max-results N] [--timeout-ms N] [--as-of ISO]` ->
  `{"results":[{"memory_id","bank_id","revision":int,"kind","status":"candidate|verified|superseded|retracted","text","provenance":{...sparse...},"score"?}],"degraded":bool,"offline":bool,"timing_ms":int}`. Superseded/retracted are excluded by default. Per-call timeout overrides client default. Results are untrusted data.
- `retain` (existing `remember`/`summary` kept) gains `--scope`, `--provenance JSON`; result `{"outcome":"persisted|queued_durable|rejected","memory_id","bank_id","revision"}`; never `persisted` unless server-acknowledged. Idempotency key = sha256(scope, bank, evidence refs, normalized text) when `--document-id` absent.
- `correct MEMORY_ID TEXT --evidence REF` -> writes revision N+1 (`document_id` stays addressable as `<id>`, prior revision marked superseded in metadata) and invalidates caches; `forget` retained.
- `get MEMORY_ID [--scope]`, `status` (adds `healthy`, `outbox_pending`, `dead_letter`, `server_version`, `contract: 1`), `doctor`.
Provenance keys (all optional, never inferred): source_system, actor_id, harness_id, host_id, repo_id, mission_id, session_id, execution_id, commit_sha, pr_ref, evidence_refs.
Existing `state` values map: delivered->persisted, queued->queued_durable, cancelled->rejected.

## Handoff `memory_context` (optional, additive, `schema` stays compatible)
`{"version":1,"refs":[{"memory_id","bank_id","revision"}],"snapshot_digest":"sha256:..","checkpoint":{"state":"synced|pending","outbox_pending":int,"at":ISO}}`. No memory text, no secrets.

## Rules
Retrieved memory is data, never instructions; label it so when injected. Never persist secrets/hidden reasoning. Preserve existing user configs; managed blocks are marked `herdr-memory:managed` and removable.

# DECISIONS

**D1 Inferred edges vs lineage contract.** `docs/contracts/lineage-events.md` says lineage is explicit-only and the graph has no second source of truth. Decision: inferred, confidence-scored, reviewable relationships live in `pi_relationship` (with `derivation`, `review_state`), never in `graph.py`. Explicit and human-verified links are also emitted as lineage events. UI shows provisional edges with a label. This does not violate the contract because `graph.py` stays a projection of events only.

**D2 One event log.** `fabric_events` + envelope v2 is the log. No parallel events table. Spec fields missing from v2 are carried in the payload `_pi` block; `tenant_id` maps to `org_id`. Added tables: inbox, outbox, intelligence entities only.

**D3 Tenancy.** `org_id` + `visibility_scope` on every new row and every query; plus per-project ACL. Risk recorded: the assistant's `X-Herdr-User` header is trusted. New routes do NOT trust that header; they use the API-key principal. iPhone device auth is a blocker (B4).

**D4 "Project".** Intelligence Project is a new record that consumes `project_links.py` for repo mapping. It is distinct from the collab HCN project (permission container), GitHub Projects v2 boards and the free-string `project_id`.

**D5 Retrieval cache.** Spec names OpenViking; the only OpenViking client (pi-engineering) has no delete. herdr-memory (Hindsight) has `forget()` + tombstones. Decision: define a `RetrievalSink` interface; first implementation targets herdr-memory; OpenViking can implement it once it has delete. Retrieval is never the ledger and carries only permitted fragments.

**D6 Merge path.** Divergence recorded: the gate does a direct App squash-merge with `sha`, not native auto-merge / merge queue; `ci-fix-runner` uses a PAT. Decision: add a deterministic risk-class policy evaluator in front of `decide_merge` (it can only deny/escalate, never approve); keep the direct SHA-pinned merge; auto-merge stays off. Incident state machine is a projection over `ci_fix` records and review-gate PR states — no third runner.

**D7 Local-only egress.** No data-classification exists in `llm_proxy`. New: a `data_class` gate; email/attachment-derived content may only route to local models. Default deny for anything unclassified.

**D8 Migration number.** `0020_project_intelligence.sql` (PR #304 holds 0014–0016). `-- DOWN:` SQL included per repo convention, plus a test that it parses and drops only our tables.

**D9 Flags.** `control_plane.project_intelligence.{enabled,ingest,derive,suggest,execute,auto_merge}` YAML, default off, hot-read, kill switch blocks new autonomous actions. Config is YAML only (no env vars).

**D10 UI footprint.** One capability-gated NAV and one ROUTES entry in `app.js` (dirty in the main checkout); everything else in a new view file.

**D11 Fixtures.** All fixtures synthetic. No real mailbox is read.

## Blockers needing a user action (not stubbed as done)
- B1 Google OAuth client + consent for Gmail/Calendar.
- B2 GitHub App webhook registration + public receiver URL + webhook secret file.
- B3 Transactional email channel (SMTP/API) and its credentials.
- B4 iPhone device authentication/registration scheme and Rakazo server compatibility check.
- B5 Live `/api/v1/health` capability verification (API key).
- B6 Any deploy, flag flip or migration against a live DB (user deploys).
